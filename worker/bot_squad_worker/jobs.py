"""Time-driven jobs the worker runs via APScheduler.

v1 ships only the heartbeat. Spec #3 adds deploy_monitor, oauth_refresh.
"""
from __future__ import annotations

import json as _json
import logging
import time as _time
from pathlib import Path

from bot_squad_worker.config import Config

log = logging.getLogger(__name__)

# T-0744: the wall-clock moment THIS worker process began (module import happens
# during boot, before the scheduler exists). It is the cutoff that tells an
# in-flight-restart marker written BEFORE we started — the restart that produced
# us, ours to clear once we are observable — from one written by US after we
# started, for a restart we launched and are about to be SIGTERM-ed by. Never
# clear the latter: see clear_restart_inflight.
_PROCESS_STARTED_AT = _time.time()
# Flipped by the first heartbeat that successfully writes the file. Makes "the
# marker is cleared at the FIRST successful heartbeat" literal rather than
# "on every heartbeat, incidentally".
_inflight_marker_cleared = False


def heartbeat(cfg: Config) -> None:
    """Write the worker's running git_sha into the heartbeat file so the API can
    show worker liveness AND detect API/worker sha drift at runtime (T-0456).

    T-0717: writes the EFFECTIVE sha, not the raw frozen boot sha — after a deploy
    whose commit changed nothing under ``worker/`` the running process IS on that
    commit's worker code, and reporting the frozen boot sha there produced a
    permanent false ``sha_drift`` that no restart ever cleared. See
    ``deploy.effective_worker_git_sha``.

    The mtime still freshens on every write, so the existing >300s-stale liveness
    check is unchanged. The sha lookup is guarded: a missing-git edge falls back to
    an empty body rather than losing the heartbeat (liveness must survive). The
    write is atomic (tmp + os.replace) so the API never reads a torn body.

    T-0744: this job also CLEARS the in-flight-restart marker, on its first
    successful run. The file written here IS the API's view of "the worker is
    up", so the instant it lands carrying our sha is the instant a restart has
    demonstrably landed — one step later than T-0739's clear-at-process-start,
    and the step that matters. The ordering is load-bearing and pinned by test:
    the clear sits AFTER the write and nowhere else in the worker, so a restart
    that never comes up never clears anything, its ``expected_by`` lapses, and
    health reverts to a bare ``sha_drift`` exactly as T-0739 designed. This is
    not a post-deploy grace period: nothing here is excused by elapsed time.

    T-0824: this job also publishes the INSTALL TREE's HEAD, to its own file
    (``_publish_install_tree_sha``). Note what the body written here is and is
    NOT — it is ``effective_worker_git_sha()``, i.e. what this PROCESS is
    running, and it is emphatically not the tree's sha. The two coincide on a
    converged install, which is exactly the state where the number tells you
    nothing; a health flag built on that coincidence was silent while the worker
    ran 22 stale modules.
    """
    import os

    cfg.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        from bot_squad_worker.deploy import effective_worker_git_sha

        sha = (effective_worker_git_sha() or "").strip()
    except Exception:
        log.exception("heartbeat: git_sha lookup failed; writing empty body")
        sha = ""
    tmp = cfg.heartbeat_path.with_name(cfg.heartbeat_path.name + ".tmp")
    tmp.write_text(sha + "\n")
    os.replace(tmp, cfg.heartbeat_path)
    _publish_install_tree_sha(cfg)
    _publish_worker_census(cfg)
    _clear_landed_restart(cfg)


def _publish_install_tree_sha(cfg: Config) -> None:
    """Publish the INSTALL TREE's HEAD beside the heartbeat (T-0824).

    A separate file, not a second line in the heartbeat body, and that is not a
    style choice: ``routes_health`` reads the heartbeat as ``read_text().strip()``
    — the WHOLE body is the sha. An api container predating this change reading a
    two-line body would compare "sha\\nsha" against its own and report a false
    ``sha_drift``, for the entire window between a worker restart and the next api
    rebuild. A new file is invisible to an old reader.

    Runs AFTER the heartbeat write and cannot displace it: the heartbeat is the
    liveness signal, this is a diagnostic term, and losing liveness to publish a
    diagnostic would be a strictly worse trade. ``publish_install_tree_sha`` is
    already best-effort internally; the wrapper catches the rest for the same
    reason ``_clear_landed_restart`` does.
    """
    try:
        from bot_squad_worker.deploy import publish_install_tree_sha

        publish_install_tree_sha(cfg)
    except Exception:
        log.exception("heartbeat: publishing the install tree sha failed")


def _publish_worker_census(cfg: Config) -> None:
    """Publish the PER-WORKER census beside the heartbeat (T-0880).

    The heartbeat carries ONE sha and it is this process's. Per-user workers
    (``data/_sock/user-*.sock``) run from the same install tree, are never
    restarted by a deploy, and appear in no health field — so an install could
    report converged on every signal it had while a four-day-old per-user worker
    served the path the deploy was FOR.

    Published from here, on the coordinator's existing tick, rather than probed
    by the API on demand: ``/api/health`` is polled, and fanning out a socket
    round-trip per worker per poll would make the health endpoint's latency a
    function of how many workers are wedged. It also keeps ONE implementation of
    "who is serving this install" instead of a second copy in the api container
    that could drift from this one.

    A separate file for the same reason as ``install_tree.json`` (T-0824): a new
    file is invisible to an api container that predates it, whereas widening an
    existing body breaks the old reader.

    Best-effort and last, like its neighbours — a diagnostic must never cost the
    liveness signal it sits beside.
    """
    import json
    import os
    import time

    try:
        from bot_squad_worker import worker_census
        from bot_squad_worker.deploy import install_tree_git_sha

        deployed = (install_tree_git_sha() or "").strip()
        summary = worker_census.summarize(
            worker_census.census(cfg.data_dir, deployed_sha=deployed)
        )
        body = {
            "at": time.time(),
            "deployed_git_sha": deployed,
            "all_converged": summary["all_converged"],
            "counts": summary["counts"],
            "line": summary["line"],
            "workers": summary["workers"],
        }
        path = cfg.heartbeat_path.parent / "workers.json"
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(body, indent=2, sort_keys=True))
        os.replace(tmp, path)
    except Exception:
        log.exception("heartbeat: publishing the worker census failed")


def _clear_landed_restart(cfg: Config) -> None:
    """Retire the in-flight-restart marker now that this process is observable.

    Called ONLY from ``heartbeat``, only after the write succeeded (T-0744). Best
    effort by design: failing to clear the marker costs at most one ``expected_by``
    window of a stale "restarting" label, whereas letting an exception escape
    would cost the heartbeat itself.
    """
    global _inflight_marker_cleared
    if _inflight_marker_cleared:
        return
    try:
        from bot_squad_worker.deploy import clear_restart_inflight

        clear_restart_inflight(cfg, recorded_before=_PROCESS_STARTED_AT)
    except Exception:
        log.exception("heartbeat: clearing the in-flight restart marker failed")
        return
    _inflight_marker_cleared = True


def deploy_monitor(cfg: Config) -> None:
    """Sweep ALL registered projects (serial), running each one's deploy tick.

    Retained as a manual all-projects sweep + the test entrypoint. Production
    no longer schedules THIS as a single job: T-0212 registers one
    ``deploy_monitor_one`` apscheduler job PER PROJECT so a hung build can't
    head-of-line-block the others (see scheduler.build_scheduler). Per-project
    exceptions are caught and logged; one bad project doesn't kill the sweep.
    """
    for slug in cfg.projects:
        try:
            deploy_monitor_one(cfg, slug)
        except Exception:
            log.exception("deploy_monitor: unhandled error for project %s", slug)


def deploy_monitor_one(cfg: Config, slug: str) -> None:
    """Run the deploy lifecycle for ONE project: reap stale orphans, then run
    the next queued deploy.

    Registered as its own apscheduler job per project (``max_instances=1``),
    so a wedged build in project A holds only A's instance — sibling projects'
    jobs run independently on their own scheduler threads (T-0212 per-project
    isolation). The two stages are guarded separately so a reaper error never
    skips the deploy and vice-versa.
    """
    project = cfg.projects.get(slug)
    if project is None:
        return
    # T-0243: reconcile FINISHED-but-orphaned processing/ markers to their ACTUAL
    # recorded rc FIRST — so a deploy that succeeded but lost its _finish move to
    # a worker-restart race lands in processed/.ok, instead of being blindly
    # age-failed by the reaper below. Runs every tick (incl. the post-restart
    # startup tick), guarded separately.
    try:
        from bot_squad_worker import deploy as _deploy
        reconciled = _deploy.reconcile_finished_orphans(cfg, slug)
        if reconciled:
            log.info("deploy_monitor: %s reconciled %d finished orphan(s): %s",
                     slug, len(reconciled), [r["queue_id"] for r in reconciled])
    except Exception:
        log.exception("deploy_monitor: orphan reconcile failed for %s", slug)
    try:
        _reap_project_orphans(cfg, slug, project)
    except Exception:
        log.exception("deploy_monitor: orphan reap failed for %s", slug)
    # T-0335 item-13: surface failed detached worker-restarts. Runs BEFORE the
    # deploy stage's empty-queue early-return so a FAIL marker is alerted even
    # when nothing is queued — a failed auto-restart otherwise left the worker
    # silently on old code with no alert.
    try:
        _surface_worker_restart_fails(cfg, slug, project)
    except Exception:
        log.exception("deploy_monitor: worker-restart FAIL surface failed for %s", slug)
    try:
        _run_project_deploy(cfg, slug, project)
    except Exception:
        log.exception("deploy_monitor: deploy run failed for %s", slug)
    # next-wave #11 (T-0451): keep-last-N retention prune of the deploy job
    # archive (processed/ + runs/ only ever grow). Separately guarded + never
    # raises, so a prune error can't skip a deploy or wedge the monitor.
    try:
        from bot_squad_worker import deploy as _deploy
        pruned = _deploy.prune_processed_runs(cfg, slug)
        if pruned.get("processed_pruned") or pruned.get("runs_pruned"):
            log.info(
                "deploy_monitor: %s pruned deploy archive (processed=%d, runs=%d, kept=%d)",
                slug, pruned["processed_pruned"], pruned["runs_pruned"], pruned["kept"],
            )
    except Exception:
        log.exception("deploy_monitor: deploy archive prune failed for %s", slug)


def _reap_project_orphans(cfg: Config, slug: str, project: object) -> None:
    """Sweep stale processing/ orphans for ``slug`` and alert the operator.

    A run stranded in processing/ (crashed worker, or a pre-T-0212
    TimeoutExpired) is moved to processed/.fail and a TARGETED operator alert
    fires — so a wedged run can't linger for days unnoticed (the 2-week-old
    watchrobot orphans the ticket cites).
    """
    from bot_squad_worker import deploy as _deploy

    reaped = _deploy.reap_orphans(cfg, slug)
    for orphan in reaped:
        hrs = (orphan.get("age_seconds") or 0) / 3600.0
        _alert_operators(
            cfg, slug, project,
            f"🧟 deploy ORPHAN reaped — {slug}/{orphan.get('target')} job "
            f"{orphan.get('queue_id')} sat in processing/ for {hrs:.1f}h "
            f"(crashed/killed run, never finished) → swept to "
            f"processed/.fail.{_deploy.RC_ORPHAN}. The queue is now unblocked.",
        )


def _surface_worker_restart_fails(cfg: Config, slug: str, project: object) -> None:
    """Alert the operator about any failed detached worker-restart (T-0335 #13).

    ``deploy.surface_worker_restart_fails`` finds + tombstones each
    ``.worker-restart.FAIL`` marker (so it alerts exactly once); we turn each
    into a TARGETED operator alert — a failed auto-restart means the worker is
    still on OLD code and needs a hand-restart.
    """
    from bot_squad_worker import deploy as _deploy

    for fail in _deploy.surface_worker_restart_fails(cfg, slug):
        tail = (fail.get("tail") or "").strip()
        _alert_operators(
            cfg, slug, project,
            f"⚠️ worker-restart FAILED — {slug} deploy {fail.get('queue_id')}: the "
            f"post-deploy worker restart did not complete, so the worker is still "
            f"running OLD code. Restart by hand: systemctl --user restart "
            f"bot-squad-worker.service\n\nlog tail:\n{tail}",
        )


def _run_project_deploy(cfg: Config, slug: str, project: object) -> None:
    """Pop and run one deploy for ``slug``, sending TG pings.

    The "🚚 starting" ping is gated on the per-target tree being clean —
    without that gate, the monitor pings at 60s cadence forever when a
    dirty tree blocks the queue, and the operator gets spam with no
    success/fail follow-up (root cause of the deploy_monitor spam reported
    2026-05-18). Dirty-deferred state is logged, not pinged.
    """
    from bot_squad_worker import channels as _channels
    from bot_squad_worker import deploy as _deploy

    queued = _deploy.list_queued(cfg, slug)
    if not queued:
        return

    # Pause gate (T-0094) — checked before everything else. When the
    # PAUSED.json marker exists, defer silently. The TG ping at pause
    # time already told the operator; no per-tick reminder spam.
    if _deploy.is_paused(cfg, slug) is not None:
        log.info("deploy_monitor: %s deferred — PAUSED", slug)
        return

    # Peek at the oldest queued job's target so we can run the per-target
    # cleanliness check before any user-visible TG ping. (run_next() will
    # re-read the file anyway; this peek doesn't move anything.)
    try:
        peek = _json.loads(queued[0].read_text())
        target = peek.get("target")
    except Exception:
        log.exception("deploy_monitor: %s could not read queue head %s", slug, queued[0])
        return
    if not target:
        log.warning("deploy_monitor: %s queue head %s has no target", slug, queued[0])
        return

    if not _deploy.is_clean_for_target(cfg, slug, target):
        # Tree is dirty — defer silently. No ping yet; we'll pick it up
        # again next tick once a peer commits or stashes their WIP.
        log.info("deploy_monitor: %s/%s deferred — tree dirty", slug, target)
        return

    # T-0490: deploy notifications are a TECHNICAL DELIVERY routed through the
    # channel abstraction (not a direct tg.send). The factory picks the impl per
    # project — TG today; adding MAX/mail needs no change here.
    channel = _channels.get_channel(cfg, project=slug)
    from bot_squad_worker import msg_routes as _msg_routes
    chat_id = project.tg_chat  # type: ignore[attr-defined]
    sid = "deploy_monitor"
    # T-0386: route deploy logs into the project's #deploy-logs forum topic
    # (None when unprovisioned → the group's general feed, i.e. no regression).
    from bot_squad_worker import tg_topics as _tg_topics
    deploy_topic = _tg_topics.resolve(cfg, slug, "deploy_logs")

    # Tree is clean — ping at start and at finish. TG outages must not
    # block the deploy itself: api.telegram.org went unreachable from this
    # host once (IPv6 default with no route) and the uncaught network error
    # propagated out of deploy_monitor, leaving queued recipes wedged in
    # the queue dir indefinitely. Swallow + log; the deploy queue is the
    # source of truth, the TG ping is best-effort observability.
    #
    # urgent=True (T-0188): deploy start/finish are project-bound SYSTEM
    # notifications, not idle-DM flood — they must fire by default for any
    # project with a tg_chat set, INCLUDING during quiet hours. Without this
    # flag the quiet-hours gate (17–05 UTC ≈ the stakeholder's whole Tashkent
    # evening/night) silently dropped every deploy alert: the regression the
    # stakeholder reported as "no more alerts from @bot_squad_bot". Same
    # precedent as autoupdate_apply's apply-failure ping.
    from bot_squad_worker import sessions as _sessions
    sid_label = _sessions.sid_display_label(sid, slug)

    # T-0799: the destination is per-message-TYPE configurable, and this one
    # sender emits BOTH classes — a start/success is a log entry, a failure or a
    # stale worker is not. So `msg_type` is a required argument rather than a
    # property of the sender: classifying by "which subsystem emitted this"
    # would put ✅ SUCCESS on the same siren as ❌ FAILED, which is exactly what
    # the stakeholder is trying to escape.
    #
    # `urgent=True` (T-0188) is UNCHANGED for every one of them, including the
    # LOG-class ones. urgent= is the quiet-hours BYPASS and has never selected a
    # destination, so a 03:00 deploy notice still fires — it fires wherever the
    # type is routed. T-0188 was about the gate DROPPING these; nothing here
    # re-introduces a drop.
    def _tg_safe(text: str, msg_type: str) -> None:
        routed = _msg_routes.route(
            cfg, msg_type, slug=slug, chat_id=chat_id, topic_id=deploy_topic,
        )
        try:
            channel.send(text, chat_id=routed.chat_id, sid=sid_label, urgent=True,
                         topic_id=routed.topic_id)
        except Exception:
            log.exception("deploy_monitor: channel.send failed (non-fatal): %s", text)

    _tg_safe(f"🚚 starting deploy for {slug}/{target}", "deploy_status")

    result = _deploy.run_next(cfg, slug)
    if result is None:
        # Race: tree went dirty between peek and run_next, or queue emptied.
        log.info("deploy_monitor: %s deferred between peek and run_next", slug)
        return

    suffix = (
        f" ({result.collapsed_count} queued requests collapsed)"
        if result.collapsed_count > 1
        else ""
    )
    # T-0919 turned the 1800s wall-clock cap from a KILL into a NOTICE, so a
    # slow-but-working build now runs to completion. That is only an improvement
    # if somebody is told it happened — an unreported notice is the cap silently
    # deleted. Folded into `suffix` deliberately: `suffix` is carried by EVERY
    # terminal line below, so the notice cannot go missing on the branch nobody
    # thought to wire it into, and it reaches the case that matters most — a
    # deploy that crossed the budget and then SUCCEEDED, which no failure path
    # would ever have reported.
    #
    # The KILLED branch is the one exception, and it takes `collapsed` instead:
    # "was allowed to continue" reads as a contradiction inside a sentence that
    # says the run was killed, so that branch states the same fact in its own
    # words and in the right ORDER (crossed the budget, kept going, then died).
    collapsed = suffix
    if result.budget_exceeded:
        suffix += (
            f" — ⚠️ this run passed its {_secs(result.budget_s)} wall-clock budget "
            f"and was allowed to continue (the budget is a notice, not a kill)"
        )
    # T-0959: a release that LEFT COMMITTED WORK BEHIND says so here. The
    # omission was already detected and already logged at WARNING — it fired on
    # every deploy from 2026-08-18 to 2026-09-06 and reached nobody, because the
    # journal is not a human. Folded into `suffix` for the same reason the budget
    # notice is: `suffix` rides EVERY terminal line below, including the plain
    # green SUCCESS, which is precisely the branch where "your commit did not
    # ship" must not be missing. Counted by content, so a clone that has merely
    # diverged (patch-equivalent SHAs, all shipping) stays silent.
    if result.omitted_count:
        first = result.omitted_commits.splitlines()[:3]
        more = result.omitted_count - len(first)
        named = "; ".join(c[:80] for c in first) + (f"; +{more} more" if more > 0 else "")
        suffix += (
            f" — ⚠️ this release OMITTED {result.omitted_count} committed change(s) "
            f"that are not on origin: {named}. They are NOT on the install. "
            f"Push them and re-deploy."
        )
    if result.ok:
        # T-0446: echo WHICH commit shipped + the worker-restart decision, so a
        # green SUCCESS that precedes the async detached restart is self-explaining
        # (no false stale-worker panic — the T-0436 operational residue).
        sha = f" @{result.resolved_sha[:12]}" if result.resolved_sha else ""
        wr = f" — worker restart: {result.worker_restart_status}" if result.worker_restart_status else ""
        # T-0880: the restart above is the COORDINATOR's. Carry what happened to
        # the workers it cannot reach into the SAME line, because this ping is
        # where "release deployed" gets read.
        pu = f" — {result.per_user_workers}" if result.per_user_workers else ""
        if result.worker_stale:
            # T-0717 leg 3: the recipe succeeded but the worker is still running the
            # PREVIOUS commit's code, so a green ✅ would be a lie — an operator who
            # trusts it ships a worker fix that isn't executing and gets no signal
            # at all (the T-0717 verbatim complaint). Say it plainly, name both
            # shas, and give the one-liner that fixes it now rather than in ~5min.
            boot = f" running={result.worker_boot_sha[:12]}" if result.worker_boot_sha else ""
            # Only a DEFERRED restart self-converges; a no-systemd-scope skip never
            # will, so don't promise a recovery that isn't coming.
            fix = (
                "It converges on its own within ~5min (deferred restart), or now: "
                if "deferred" in result.worker_restart_status
                else "This will NOT self-correct — run: "
            )
            _tg_safe(
                f"⚠️ deploy {slug}/{target} SUCCESS but WORKER STALE "
                f"(rc={result.returncode}){sha}{suffix}{wr}. The worker is still on"
                f"{boot or ' the previous commit'} — new worker/ code is NOT executing "
                f"yet. {fix}systemctl --user restart bot-squad-worker.service{pu}",
                # A green recipe whose worker is still on the previous commit is
                # a FAILURE for every purpose he cares about — the fix is a
                # command he has to run (T-0717). Classified with the failures,
                # not with the successes it is printed next to.
                "deploy_failed",
            )
        elif result.per_user_workers_stale:
            # T-0880: the recipe succeeded and the coordinator was handled, but a
            # per-user worker is still on the replaced code (or could not say what
            # it is on). A bare ✅ here is the exact reading that let a four-day-old
            # worker serve the path this deploy was for. Not classified as a
            # failure — the deploy DID succeed and declining the bounce is by
            # design — but it must not look unqualified.
            _tg_safe(
                f"⚠️ deploy {slug}/{target} SUCCESS, COORDINATOR ONLY "
                f"(rc={result.returncode}){sha}{suffix}{wr}{pu}",
                "deploy_status",
            )
        else:
            _tg_safe(
                f"✅ deploy {slug}/{target} SUCCESS (rc={result.returncode}){sha}{suffix}{wr}{pu}",
                "deploy_status",
            )
    elif result.returncode == _deploy.RC_CLONE_WEDGED:
        # T-0453: the recipe never ran — the deploy clone could not be synced, and
        # the old answer was to retry silently once a minute forever. This is the
        # loud path a failed deploy already uses, plus a direct word to the
        # requester, who is otherwise holding an {"ok": true, queue_id} that
        # nothing will ever contradict.
        n = result.collapsed_count
        also = f" ({n} queued job(s) failed together — all blocked by the same clone)" if n > 1 else ""
        text = (
            f"❌ deploy {slug}/{result.target or target} WEDGED — the deploy clone could "
            f"not be synced to origin, so the recipe never started (rc={result.returncode})"
            f"{also}. Retrying was stopped rather than continued: this does not clear on "
            f"its own. The queue is now unblocked.\n\n{result.failure_detail}"
        )
        _alert_operators(cfg, slug, project, text)
        _notify_requester(cfg, slug, result.requested_by, text)
    elif result.killed_reason:
        # A watchdog (not the recipe) killed this build — the loud, TARGETED
        # operator alert path (T-0212), not the routine project-channel ping.
        #
        # T-0920. The branch itself is not new (T-0839's "a killed deploy
        # presents as a plain recipe failure" is half wrong, and saying so is
        # part of that ticket) — what it said was. In full, for the 1802s kill
        # of run 311ba682 on 2026-08-18 13:18Z:
        #
        #   ❌ deploy bot-squad/staging KILLED by hard timeout (build ran too
        #   long) (rc=124). The build was terminated and the queue is now
        #   unblocked. Log: …/311ba682-….log
        #
        # Three things wrong with that, in rising order of cost. It carries no
        # numbers — not the elapsed time, not the limit, not what had run. It
        # quotes nothing of the log, so every reader has to go and tail it,
        # which is the same defect T-0878 fixed one branch down. And "build ran
        # too long" is a WRONG DIAGNOSIS: the log was still growing when the
        # kill landed (npm run build had been printing 54s earlier), so it
        # points the reader at a slow build step when the actual cause was a
        # wall-clock budget consumed by host I/O contention.
        #
        # It also told the REQUESTER nothing. Every other bad-deploy path here
        # calls _notify_requester (T-0453, restated for rc!=0 by T-0878); the
        # watchdog branch was the last failure path still leaving the session
        # that asked holding an {"ok": true, queue_id} nothing would contradict.
        text = _killed_deploy_report(slug, result.target or target, result, collapsed)
        _alert_operators(cfg, slug, project, text)
        _notify_requester(cfg, slug, result.requested_by, text)
    else:
        # T-0878 DoD 5. Until now this branch said exactly
        # "❌ deploy bot-squad/staging FAILED rc=8" — no reason, no remediation,
        # and nothing to the session that asked. The two measured rc=8 refusals
        # (2026-08-06 08:27, 2026-08-11 14:23) both fired this line and both
        # reached nobody who acted; the install stayed undeployable for five
        # days and the defect was found by accident. Three changes, all here:
        # carry the recipe's own FATAL block so no one has to tail a run log,
        # tell the requester (who is holding an {"ok": true, queue_id} nothing
        # else will ever contradict — the T-0453 principle, never applied to
        # this path), and escalate a pre-flight REFUSAL to the loud operator
        # channel because it re-fires identically on every retry.
        detail = _recipe_failure_detail(result.log_path)
        refusal = result.returncode in RECIPE_REFUSAL_RCS
        head = (
            f"❌ deploy {slug}/{target} REFUSED before shipping anything "
            f"(rc={result.returncode}){suffix} — this does NOT clear on its own; "
            f"every retry fails identically until a human clears it."
            if refusal
            else f"❌ deploy {slug}/{target} FAILED rc={result.returncode}{suffix}"
        )
        text = head + (f"\n\n{detail}" if detail else "") + f"\n\nLog: {result.log_path}"
        _tg_safe(text, "deploy_failed")
        _notify_requester(cfg, slug, result.requested_by, text)
        if refusal:
            _alert_operators(cfg, slug, project, text)


#: Recipe exit codes that mean the deploy was REFUSED at pre-flight — a state a
#: human has to clear — rather than a build that might pass on the next try.
#: Read off deploy-recipes/bot-squad/{staging,prod}.sh: 3 wrong branch, 4 dev
#: clone diverged, 5 install diverged, 7 local-only install commits, 8 dirty
#: install, 10 T-0878 migration assertion. Build/verify failures (6 no image,
#: 9 running sha != deployed sha) are deliberately NOT here: they can pass on a
#: retry, so they get the ordinary channel notice.
#:
#: Another project's recipe is free to use these numbers for something else.
#: The cost of that collision is one extra operator alert — never a dropped
#: one — which is the direction this ticket wants the error to lean.
RECIPE_REFUSAL_RCS = frozenset({3, 4, 5, 7, 8, 10})

#: How much of the run log to carry into the alert when no FATAL line is found.
_LOG_TAIL_LINES = 12
#: Hard cap on the quoted block, so a runaway log can't blow the message limit.
_DETAIL_MAX_CHARS = 1200


def _recipe_failure_detail(log_path: object) -> str:
    """The part of the run log a human needs, quoted into the alert (T-0878).

    Prefers the recipe's own ``FATAL`` block — every refusal in the bot-squad
    recipes prints ``FATAL:`` followed by the specifics and the remediation, and
    that block IS the diagnosis. Falls back to the log tail when a recipe fails
    without one (a compose/build error). Best-effort: an unreadable log degrades
    to an empty string, which only costs the alert its detail — never the alert.
    """
    if not log_path:
        return ""
    try:
        text = Path(str(log_path)).read_text(errors="replace")
    except OSError:
        return ""
    lines = text.splitlines()
    if not lines:
        return ""
    fatal_at = next(
        (i for i in range(len(lines) - 1, -1, -1) if "FATAL" in lines[i]), None
    )
    block = lines[fatal_at:] if fatal_at is not None else lines[-_LOG_TAIL_LINES:]
    out = "\n".join(block).strip()
    if len(out) > _DETAIL_MAX_CHARS:
        out = out[:_DETAIL_MAX_CHARS] + "\n… (truncated — full log above)"
    return out


#: Human term for each watchdog, used when the result does not carry one of its
#: own. ``killed_limit_name`` is populated by T-0919; a run recorded before it
#: (or after a rollback) has "", and a kill must still name what killed it.
_KILL_LIMIT_NAMES = {
    "no_progress": "no-progress budget",
    "ceiling": "absolute ceiling",
    # RETIRED by T-0919 — no new run produces it. Kept because the archive is
    # full of records that do, and a rollback would produce it again.
    "timeout": "wall-clock timeout",
}


def _killed_deploy_report(slug: str, target: str, result: object, suffix: str) -> str:
    """What a human is told when a WATCHDOG, not the recipe, ended a deploy.

    Built around one number: **how long the run log had been silent when the run
    ended** (``silence_s``). That is the whole discriminator, and it is why this
    function exists rather than a second wording of the old line.

    T-0919 measured all 100 run logs on this host. The longest silent stretch on
    a live, healthy build was **583.7s** — the 600s no-progress budget was 16
    seconds from killing a build that was working. So "the log went quiet" does
    NOT prove a wedge, and "the run was long" does not prove a slow step. Only
    the two numbers side by side separate *we killed a working build* from *we
    killed a dead one*, and until now neither reached the reader.

    Every clause is conditional on its evidence existing. A run recorded before
    T-0919 carries zeros for all of it, and a zero rendered as "0.0s silent"
    would read as a real measurement of a wedged build — the exact inversion of
    the fact. So the absence is stated as an absence instead (T-0453's principle
    applied to a number: an unpopulated field must never be printed as a value).
    """
    reason = str(result.killed_reason or "")
    limit_name = result.killed_limit_name or _KILL_LIMIT_NAMES.get(reason, "watchdog")
    elapsed = float(result.killed_elapsed_s or 0.0)
    limit_s = int(result.killed_limit_s or 0)
    # `killed_elapsed_s` is the presence flag for the whole telemetry group: a
    # run that was killed necessarily ran for a non-zero time, so 0.0 here can
    # only mean "this result predates the fields", never "it ran for no time".
    measured = elapsed > 0

    head = (
        f"❌ deploy {slug}/{target} KILLED by the {limit_name} "
        f"(rc={result.returncode}){suffix} — a WATCHDOG ended this run. The "
        f"recipe did not fail and the build did not error."
    )

    lines = [head, ""]
    if measured:
        limit = f" against a limit of {_secs(limit_s)}" if limit_s > 0 else ""
        lines.append(f"Ran {_secs(elapsed)}{limit}.")
        lines.append(_kill_liveness_clause(reason, float(result.silence_s or 0.0)))
    else:
        lines.append(
            "Elapsed time, the limit that fired and the log-silence at the kill "
            "are NOT RECORDED for this run — it predates the watchdog telemetry. "
            "Read the log below rather than assuming which limit this was."
        )

    progress = _completed_work_clause(result)
    if progress:
        lines.append(progress)

    if result.budget_exceeded:
        # Stated AFTER the liveness clause so it reads in the order it happened:
        # the run crossed its wall-clock budget, was deliberately allowed to keep
        # going (T-0919), and only then hit whatever finally killed it.
        lines.append(
            f"It had already passed its {_secs(result.budget_s)} wall-clock budget "
            f"before this kill and was deliberately allowed to continue — that "
            f"budget is a notice, not the limit that ended the run."
        )

    drift = _install_drift_clause(result)
    if drift:
        lines.append("")
        lines.append(drift)

    lines.append("")
    lines.append("The queue is now unblocked.")
    detail = _recipe_failure_detail(result.log_path)
    if detail:
        lines.append("")
        lines.append(detail)
    lines.append("")
    lines.append(f"Log: {result.log_path}")
    return "\n".join(lines)


def _kill_liveness_clause(reason: str, silence_s: float) -> str:
    """Was the build ALIVE when it was killed? — the sentence T-0920 is for.

    Stated from the measurement rather than from the reason code, because the
    reason code is what was wrong before: "hard timeout" was rendered as "build
    ran too long" on a run whose log was still being written to.
    """
    if reason == "no_progress":
        return (
            f"The run log had produced nothing for {_secs(silence_s)} when it was "
            f"killed — the build was WEDGED, which is what this budget exists to "
            f"catch. Note buildkit prints nothing at all during a COPY step, so a "
            f"long silence is not by itself proof the build was doing nothing."
        )
    # A wall-clock kill (the retired "timeout", or the "ceiling" that replaced
    # it). Here the silence is the whole story — but "short silence" needs a
    # scale, and inventing one (say, half the limit) would put a guess in a
    # sentence whose entire job is to replace a guess.
    #
    # The scale the system already uses is T-0919's no-progress FLOOR: below it,
    # a run is BY THIS SYSTEM'S OWN RULE not considered wedged — that is what the
    # floor means. So a wall-clock kill whose silence never reached the floor
    # killed a build the wedge-detector would have left alone. Read from
    # deploy.py rather than restated here, so raising the floor moves this
    # sentence with it instead of leaving a second, stale copy of the number.
    from bot_squad_worker.deploy import DEFAULT_NO_PROGRESS_FLOOR

    if silence_s < DEFAULT_NO_PROGRESS_FLOOR:
        return (
            f"The run log was still being written to — the last line landed "
            f"{_secs(silence_s)} before the kill, so the build was ALIVE and was "
            f"ended by a WALL-CLOCK budget, not by anything that hung. Do NOT go "
            f"looking for a slow build step: a budget exhausted by host slowness "
            f"(I/O contention, load) looks exactly like this, and the same build "
            f"finishes in minutes on a quiet box."
        )
    return (
        f"The run log had been silent for {_secs(silence_s)} when the limit fired."
    )


def _completed_work_clause(result: object) -> str:
    """"…and say what had completed" (T-0839 item 2), from T-0919's fields."""
    parts: list[str] = []
    if result.completed_steps:
        parts.append(f"{result.completed_steps} build step(s) had completed.")
    if result.last_completed_step:
        parts.append(f"Last finished: {result.last_completed_step}")
    if result.last_step:
        parts.append(f"In flight when it died: {result.last_step}")
    return "\n".join(parts)


def _install_drift_clause(result: object) -> str:
    """The invisible intermediate state T-0839 item 3 named, now MEASURED.

    T-0919's evidence for run 311ba682: the install tree's ff-merge landed at
    12:48:15Z, 13 seconds into a run that was killed at 13:18:08Z — so 99.3% of
    the kill window sits after the sync. And ``_should_restart_worker`` returns
    False whenever ``ok`` is False, so a kill ALWAYS skips the restart. This is
    not a rare race; it is the default outcome of killing a deploy, and per
    T-0824 ``/api/health`` has no term for the install tree, so nothing else
    surfaces it.
    """
    if not result.install_sha_drift:
        return ""
    sha = f" (now at {result.install_tree_sha[:12]})" if result.install_tree_sha else ""
    return (
        f"⚠️ The install tree was ALREADY updated{sha} before the kill, and a kill "
        f"always skips the worker restart — so the worker is running the OLD code "
        f"against the NEW tree and nothing will correct that on its own. Fix now: "
        f"systemctl --user restart bot-squad-worker.service"
    )


def _secs(value: float) -> str:
    """``1802.0`` → ``"1802s (30m2s)"``. Both, because the raw number is what
    matches the log and the limit, and the human one is what makes 1802 vs 1800
    legible at a glance."""
    total = int(round(float(value)))
    if total < 60:
        return f"{total}s"
    return f"{total}s ({total // 60}m{total % 60}s)"


def _notify_requester(cfg: Config, slug: str, requested_by: str, text: str) -> None:
    """Tell the SESSION that asked for the deploy that it did not happen (T-0453).

    The operator alert is not enough on its own: the requester got
    ``{"ok": true, queue_id: …}`` and has every reason to believe a deploy is
    coming. Best-effort and never raises — a dead pane must not turn a reported
    failure into an unreported one. Skipped when the requester is not a session
    SID (CLI/API callers, cron) or is the deploy monitor itself.
    """
    if not requested_by or not str(requested_by).startswith("S-"):
        return
    from bot_squad_worker import intersession as _is

    try:
        _is.send_notice(cfg, slug, "S-deploy_monitor", requested_by, text)
    except Exception:
        log.exception("deploy_monitor: requester notice to %s failed (non-fatal)", requested_by)


def _alert_operators(cfg: Config, slug: str, project: object, text: str) -> None:
    """Loud, TARGETED deploy alert (T-0212) — never a broadcast.

    Two targeted channels, mirroring the telemetry alert guardrail (T-0210):
      1. an urgent personal page via the _send_stakeholder_dm SSOT (P2-04) —
         MAX-primary on this DPI-blocked host (a raw TG send silently dropped the
         wedged-build alert here), with a best-effort #deploy-logs group-record;
         urgent so the quiet-hours gate can't drop it;
      2. a peer_send to each operator-role SID for the project.
    Both are best-effort; a channel outage or a missing operator pane never raises.
    """
    from bot_squad_worker.actions import _send_stakeholder_dm

    chat_id = getattr(project, "tg_chat", "") if project else ""
    if chat_id:
        try:
            from bot_squad_worker import tg_topics as _tg_topics
            _send_stakeholder_dm(
                cfg, message=text, sid="deploy_monitor", urgent=True,
                tg_chat_id=chat_id,
                tg_topic_id=_tg_topics.resolve(cfg, slug, "deploy_logs"),
                group_record=True,
                slug=slug,
                # T-0799: URGENT class. This helper is the loud path for EVERY
                # way a deploy goes wrong — a watchdog kill, a reaped orphan, a
                # failed worker-restart — so one type covers all of its callers.
                # `urgent=True` above unchanged.
                msg_type="deploy_failed",
            )
        except Exception:
            log.exception("deploy_monitor: operator alert failed (non-fatal): %s", text)
    try:
        _peer_to_operators(cfg, slug, text)
    except Exception:
        log.exception("deploy_monitor: operator peer_send failed (non-fatal)")


def _peer_to_operators(cfg: Config, slug: str, text: str) -> None:
    """Targeted peer_send to each LIVE operator SID for ``slug`` (no broadcast).

    T-0920 — resolution moved from a hand-rolled ``list_sessions`` walk to
    ``dispatch.live_operator_sids``, the operator-identity SSOT (T-0523), and the
    reason is a measurement rather than a tidiness argument.

    ``list_sessions`` returns EVERY session md ever written for the project,
    archived ones included, so ``role == "operator"`` matched the whole history
    of the role. Measured on this install for ``bot-squad`` at 2026-08-18 20:18Z:
    **50 SIDs**, of which **one** was live. The old ``dict.fromkeys`` deduped the
    REQUESTED sids, which is the wrong end — ``intersession.send`` then resolves
    each recycled predecessor to its live successor (T-0790), and 31 of those 50
    resolve to the SAME live operator. So one alert became 31 identical lines in
    one inbox plus 18 writes to inboxes no session drains.

    That is the "~30 duplicate peer alerts" recorded on T-0839, traced on the
    2026-08-18 13:18Z ``311ba682`` kill: 50 sends in one tick (13:18:06–13:18:13),
    32 of them into ``S-almdudleer-operator-p381``. It is NOT a legitimate fanout
    to N operators and it is NOT re-firing across ticks — the following
    ``deploy_monitor_one`` ticks at 13:18:40 and 13:19:40 emitted nothing.

    The SSOT is also a superset of what the old walk found live: it adds the
    canonical md-less operator pane a session-md scan misses (T-0523). Reaching
    NOBODY is reported rather than papered over — the alert has already gone out
    on the stakeholder DM leg, and a silent zero here is the thing that would let
    a broken roster look like a delivered alert.
    """
    from bot_squad_worker.dispatch import live_operator_sids
    from bot_squad_worker import intersession as _is

    try:
        sids = live_operator_sids(cfg, slug)
    except Exception:
        log.exception("deploy_monitor: live_operator_sids failed for %s", slug)
        return
    if not sids:
        log.warning(
            "deploy_monitor: no LIVE operator session for %s — this alert reached "
            "no operator pane (the stakeholder DM leg still fired): %.200s",
            slug, text,
        )
        return
    for sid in dict.fromkeys(sids):  # dedupe, preserve order
        try:
            # T-0827: send_notice — a deploy alert is machine-composed and
            # this loop has nobody to report a refusal to. Splits, never drops.
            _is.send_notice(cfg, slug, "S-deploy_monitor", sid, text)
        except Exception:
            log.exception("deploy_monitor: peer_send to %s failed (non-fatal)", sid)


def tg_listener_tick(cfg: Config) -> None:
    """Poll Telegram for incoming replies and route them into sessions.

    Scheduled every 30s by APScheduler. Errors are caught and logged so
    one bad update cycle never kills the scheduler.
    """
    from bot_squad_worker import tg_listener
    try:
        tg_listener.tick(cfg)
    except Exception:
        log.exception("tg_listener_tick error")


def autoupdate_tick(cfg: Config) -> None:
    """Poll the mothership release feed and queue apply jobs when newer.

    Scheduled at ``BOT_SQUAD_AUTOUPDATE_INTERVAL_SECONDS`` (default 900s)
    by APScheduler. Errors are caught and logged so one bad poll cycle
    never kills the scheduler. See ``bot_squad_worker.autoupdate`` for
    the full contract (T-0083). No-op on the mothership itself (T-0086).
    """
    from bot_squad_worker import autoupdate as _autoupdate
    try:
        _autoupdate.tick(cfg)
    except Exception:
        log.exception("autoupdate_tick error")


def autoupdate_apply_tick(cfg: Config) -> None:
    """Drain the apply queue produced by the poller (T-0084).

    Scheduled at a shorter cadence than the poll tick so a freshly-enqueued
    release starts applying within ~1 minute. The apply itself can take
    many minutes (download + build + smoke); APScheduler ``max_instances=1``
    on the registered job keeps overlapping ticks from starting a second
    apply mid-flight. No-op on the mothership itself (T-0086).
    """
    from bot_squad_worker import autoupdate_apply as _apply
    try:
        _apply.tick(cfg)
    except Exception:
        log.exception("autoupdate_apply_tick error")


def worker_restart_catchup_tick(cfg: Config) -> None:
    """T-0717 leg 2: fire a worker restart the rate limiter deferred, once safe.

    Cheap no-op in the steady state (one stat of the pending marker), so a 60s
    cadence is fine. All the decision logic — already-converged, deploy in flight,
    window not yet elapsed — lives in ``deploy.catchup_deferred_worker_restart``.
    """
    from bot_squad_worker import deploy as _deploy
    try:
        _deploy.catchup_deferred_worker_restart(cfg)
    except Exception:
        log.exception("worker_restart_catchup_tick error")


def binding_gc_tick(cfg: Config) -> None:
    """T-0072/0073/0077/0142/0144: run the binding-graph reconcilers per project.

    Ordered passes per tick (gc + reconcile, then T-0151 guidance harvest):
      1. ``gc_sessions`` — flip ``status: active`` SessionMds with no live pane
         to ``status: suspended`` so the on-disk graph matches reality.
      1b. ``reconcile_primary_from_history`` (T-0525) — repair a cross-wired /
         unbound LIVE session primary from the authoritative ticket
         ``session_history`` (the in-place repair ``bind_task`` can't do). Runs
         before the dead/stale passes so a primary cross-wired onto a closed
         ticket is rewritten to its real home rather than stripped, and the
         downstream passes see a consistent primary (no oscillation).
      2. ``gc_dead_bindings`` (T-0142) — refresh bindings from disk: strip
         ``task_id`` for closed/missing tasks and ``initiative`` for missing
         initiative files (fixes suspended devs showing stale task_ids).
      3. ``gc_stale_bindings`` — strip duplicate-claim primary ``task_id`` from
         losers in a dup race (preserve old value as ``last_task_id``).
      4. ``archive_dead_teammates`` (T-0142/0144) — auto-archive cleanly-exited
         post-totest devs (and verified-done live devs); makes dev zombies
         impossible without a TL lifting a finger. T-0202: also trims a live
         idle dev whose binding pass 2 just cleared, via ``last_task_id``.
         T-0233: also reaps an *exited* dev whose task is still open but whose
         one-time run has been dead/idle past ``BOT_SQUAD_SESSION_STALE_SEC``
         (default 24h) — a crashed/abandoned run; the binding is preserved as
         ``last_task_id`` so the still-open task is re-dispatchable.
      5. ``reconcile_teams`` (T-0142) — rebuild the tmux-session-keyed Team mds
         from the (now-reconciled) SessionMd registry so the team roster, TL
         slot, and archived members survive a worker reload.
      5b. ``backfill_parent_sid`` (T-0128) — fill ``parent_sid`` for legacy /
         agent-teams sessions via the team-projection heuristic (fill-once);
         runs after ``reconcile_teams`` so the Team md it reads is fresh.
      6. ``gc_tmux_sessions`` (T-0200) — reap idle, empty per-initiative/per-team
         tmux sessions (``<slug>-*`` with no live claude pane, idle past the
         grace) so they stop cluttering the host's ``tmux ls``. Runs after
         ``reconcile_teams`` so the durable Team md is already written before its
         (now-idle) tmux shell is removed.
      7. ``gc_orphan_tmux_sessions`` (T-0802) — ONCE per tick, after the
         per-project loop: surface (and, past a long quarantine, reap) tmux
         sessions bot-squad created for a project that is not registered. Every
         pass 1–6 is keyed on a registered slug and therefore cannot see them.

    ``gc_sessions`` runs first so the freshly-suspended sessions inform the
    stale-binding race resolution (a live-pane claimant beats a dead one);
    ``reconcile_teams`` runs last so it sees the post-archive truth.

    Per-project / per-pass errors are caught and logged so one bad project or
    pass never kills the sweep.
    """
    from bot_squad_worker import sessions as _sessions
    from bot_squad_worker import teams as _teams
    from bot_squad_worker import close_hook as _close_hook
    from bot_squad_worker import task_gc as _task_gc

    passes = [
        ("gc_sessions", _sessions.gc_sessions),
        # T-0525: repair a cross-wired / unbound LIVE session primary from the
        # authoritative ticket session_history BEFORE the dead/stale passes run —
        # so a primary cross-wired onto a closed ticket is rewritten to its real
        # home here instead of being stripped, and the downstream passes see a
        # consistent primary (stops the reconciler oscillation the ramp surfaced).
        ("reconcile_primary_from_history", _sessions.reconcile_primary_from_history),
        # T-0324: invariant enforcer — no constant-team session md ever holds a
        # primary task_id, whatever path set it (the p179→p181 cross-wire class).
        # Runs right after the history reconciler so a victim dev's primary is
        # restored first and the cross-wired copy is then stripped, same tick.
        ("reconcile_constant_team_primaries",
         _sessions.reconcile_constant_team_primaries),
        ("gc_dead_bindings", _sessions.gc_dead_bindings),
        ("gc_stale_bindings", _sessions.gc_stale_bindings),
        ("archive_dead_teammates", _sessions.archive_dead_teammates),
        # F7: archive throwaway QA tasks (QA-TEST-DELETEME-*) once closed/aged so
        # they stop leaking onto the board — the task-level analogue of the
        # T-0233 stale-session reaper in archive_dead_teammates.
        ("gc_throwaway_tasks", _task_gc.gc_throwaway_tasks),
        # T-0484: general age-based cleanup — archive stale/abandoned tasks
        # (inactive + past the long grace) off-board to backlog/_gc/ (reversible),
        # generalizing the throwaway GC into the real task-cleanup process.
        ("gc_stale_tasks", _task_gc.gc_stale_tasks),
        # T-0889 (his 2026-08-04 ask): a ticket at in_progress that NO live
        # session holds — and that nobody has touched for the grace — becomes
        # `paused`. Measured 2026-08-12: 84% of the in_progress column was
        # exactly that, a label a reaped session left behind. Ordered here on
        # purpose: gc_sessions / archive_dead_teammates / gc_dead_bindings have
        # already run this tick, so the live-holder set it reads is the
        # reconciled truth rather than last tick's.
        ("auto_pause_unheld_tasks", _task_gc.auto_pause_unheld_tasks),
        # T-0568: repair generic/blank tmux window names (the naming SSOT) from
        # the registry/binding — runs just before reconcile_teams so the roster
        # is rebuilt with the rotated SIDs in the same tick.
        ("reconcile_window_names", _sessions.reconcile_window_names),
        ("reconcile_teams", _teams.reconcile_teams),
        # T-0128: backfill parent_sid for legacy / agent-teams-spawned sessions
        # via the team-projection heuristic (fill-once, never overwrites the
        # spawn-time value). Runs after reconcile_teams so the Team md it
        # consults is freshly rebuilt this tick.
        ("backfill_parent_sid", _sessions.backfill_parent_sid),
        # T-0200: reap idle empty per-initiative/per-team tmux sessions after the
        # Team md is rebuilt, so the durable entity survives while the empty tmux
        # shell is cleaned up.
        ("gc_tmux_sessions", _sessions.gc_tmux_sessions),
        # T-0151: harvest stakeholder guidance from freshly-suspended sessions
        # into their ticket (runs after gc_sessions has flipped them suspended).
        ("harvest_guidance", _close_hook.harvest_tick),
    ]
    for slug in cfg.projects:
        for name, fn in passes:
            try:
                res = fn(cfg, slug)
                if name == "gc_stale_bindings":
                    _surface_live_dup_reconciles(cfg, slug, res)
            except Exception:
                log.exception("binding_gc_tick: %s failed for %s", name, slug)

    # T-0802: ONCE per tick, not once per project. Every pass above is keyed on
    # a registered slug, which is exactly why a tmux session belonging to NO
    # project was nobody's job for three days while it accumulated 70 live
    # claude processes. This one sweeps the sessions no slug claims.
    try:
        _surface_orphan_tmux(cfg, _sessions.gc_orphan_tmux_sessions(cfg))
    except Exception:
        log.exception("binding_gc_tick: gc_orphan_tmux_sessions failed")


def _surface_orphan_tmux(cfg: Config, result: object) -> None:
    """Alert on what the orphan sweep found — on FIRST sighting, and again when
    one is actually reaped.

    The alert is the half of T-0802 that does not depend on the reap being
    right. The quarantine is deliberately long (hours), so without this the
    operator learns nothing until the kill; with it, the first tick that sees an
    unclaimed session says so, and a human who recognises it as legitimate has
    the whole quarantine to say so. Best-effort; never raises into the tick.
    """
    if not isinstance(result, dict):
        return
    slug = "bot-squad" if "bot-squad" in cfg.projects else next(iter(cfg.projects), "")
    if not slug:
        return
    project = cfg.projects.get(slug)

    for d in result.get("sighted", []):
        panes = d.get("claude_panes") or 0
        hours = (d.get("reap_after_sec") or 0) / 3600.0
        try:
            _alert_operators(
                cfg, slug, project,
                f"⚠️ orphan tmux session — '{d.get('session')}' was created by "
                f"bot-squad (it carries the _init placeholder) but belongs to NO "
                f"registered project, and holds {panes} live claude pane(s). It "
                f"will be killed if it is still unclaimed in {hours:.0f}h. If it "
                f"is legitimate, register the project (or set "
                f"BOT_SQUAD_ORPHAN_TMUX_REAP=0); if it is not, kill it now — "
                f"every claude pane in it is burning host RAM. This is the "
                f"T-0802 class: 70 such panes once ate all of RAM + swap.")
        except Exception:
            log.exception("binding_gc_tick: orphan-tmux sighting alert failed for %s",
                          d.get("session"))

    for d in result.get("reaped", []):
        try:
            _alert_operators(
                cfg, slug, project,
                f"🧹 orphan tmux session reaped — '{d.get('session')}' had been "
                f"unclaimed by any registered project for "
                f"{(d.get('orphaned_for') or 0) / 3600.0:.1f}h, holding "
                f"{d.get('claude_panes') or 0} claude pane(s). If that project "
                f"was real, it was NOT in projects.toml at any point in that "
                f"window — re-register it before respawning (T-0802).")
        except Exception:
            log.exception("binding_gc_tick: orphan-tmux reap alert failed for %s",
                          d.get("session"))


def _surface_live_dup_reconciles(cfg: Config, slug: str, result: object) -> None:
    """T-0227: gc_stale_bindings auto-reconciles >1 sessions claiming one task_id,
    but SILENTLY — fine for the crash-only case (a dead-pane md). When the loser
    was LIVE, though, two sessions genuinely raced onto the task (the T-0218
    hazard): stripping the loser's binding does NOT stop its process — it keeps
    running claude against the SHARED worktree (the co-edit damage vector). Alert
    the operator to kill the rogue session. Best-effort; never raises into the tick.
    """
    if not isinstance(result, dict):
        return
    project = cfg.projects.get(slug)
    for d in result.get("details", []):
        if not d.get("was_live"):
            continue
        try:
            _alert_operators(
                cfg, slug, project,
                f"⚠️ duplicate-spawn auto-reconciled — {slug}/{d.get('task_id')}: two "
                f"LIVE sessions raced onto one task. Kept {d.get('winner')}; unbound "
                f"{d.get('sid')}, but that session is STILL RUNNING against the shared "
                f"worktree (co-edit hazard). Kill {d.get('sid')} — it no longer holds "
                f"the task.")
        except Exception:
            log.exception("binding_gc_tick: live-dup surface failed for %s/%s",
                          slug, d.get("task_id"))


def telemetry_tick(cfg: Config) -> None:
    """T-0210: per-project resource-telemetry sampler.

    Sibling of binding_gc_tick (its own 60s job so a slow transcript read can
    never delay the lifecycle reconcilers). Samples each LIVE session's
    context-token window + memory footprint from the Claude transcript jsonl,
    persists a small per-session record + a project quota rollup, and fires
    crossing-only urgent alerts (context warn/urgent at 0.8×/1.0× the ceiling,
    default 240k/300k — T-0210 set 700k, T-0857 lowered it, memory near cap, quota
    projected-exhaust-before-EOD, 429 throttle) to the operator + each TL.
    Per-project errors are caught and logged so one bad project never kills
    the sweep.
    """
    from bot_squad_worker import telemetry as _telemetry
    try:
        _telemetry.tick(cfg)
    except Exception:
        log.exception("telemetry_tick error")


def idle_timeout_tick(cfg: Config) -> None:
    """T-0466 / M1-F1.3: ~1h cache-window idle/waiting-session recycle.

    Sibling of telemetry_tick (its own 60s job). Recycles sessions that have
    been *waiting* (no Claude turn ⇒ cold subscription cache) past the idle
    window — asking them to record forward-state + exit via the universal-compact
    handoff — while honoring per-session postpone + auto-postpone on tracked long
    bounded jobs. No-op under ``BOT_SQUAD_IDLE_TIMEOUT=0``. Self-contained; the
    sweep swallows per-session/per-project errors so one bad session never kills
    it.
    """
    from bot_squad_worker import idle_timeout as _idle_timeout
    try:
        _idle_timeout.tick(cfg)
    except Exception:
        log.exception("idle_timeout_tick error")


def graceful_exit_tick(cfg: Config) -> None:
    """T-0465 / M1-F1.2: the uniform lifecycle's work-done → graceful-exit path.

    Suspends (NO relaunch) any active session whose assignment is DONE — a
    task-bound role whose task reached ``totest``/``closed``, or an operator whose
    backlog is empty — once it has gone quiet (post-done grace) and its pane is
    idle/composer-ready. Sibling of idle_timeout_tick (its own 60s job). No-op
    under ``BOT_SQUAD_GRACEFUL_EXIT=0``. The sweep swallows per-session/per-project
    errors so one bad session never kills it.
    """
    from bot_squad_worker import graceful_exit as _graceful_exit
    try:
        _graceful_exit.tick(cfg)
    except Exception:
        log.exception("graceful_exit_tick error")


def wait_resume_tick(cfg: Config) -> None:
    """T-0930: auto-resume a suspended WAITING session (blocked_on_user, T-0931)
    the moment its condition clears — no human needed to notice and resume it
    by hand. Sibling of idle_timeout_tick/graceful_exit_tick (its own 60s job).
    No-op under ``BOT_SQUAD_WAIT_RESUME=0``. The sweep swallows per-session/
    per-project errors so one bad session never kills it.
    """
    from bot_squad_worker import wait_resume as _wait_resume
    try:
        _wait_resume.tick(cfg)
    except Exception:
        log.exception("wait_resume_tick error")


def ticket_watch_tick(cfg: Config) -> None:
    """T-0938: fan a ticket's change out to the sessions bound to it — the push
    channel whose absence made "paste it into the operator's inbox" the only way
    to make a request visible («глухой телефон»). Detection is by polling the md
    (writer-agnostic on purpose: `bsq ticket update` and the api's PATCH never
    reach a worker action). Sibling of wait_resume_tick's 60s job. No-op under
    ``BOT_SQUAD_TICKET_WATCH=0``. The sweep swallows per-project errors so one
    bad project never kills it.
    """
    from bot_squad_worker import ticket_watch as _ticket_watch
    try:
        _ticket_watch.tick(cfg)
    except Exception:
        log.exception("ticket_watch_tick error")


def input_flush_tick(cfg: Config) -> None:
    """T-0469 / M1-F1.6: deferred-delivery pass for the input multiplexer.

    Re-attempts delivery of any per-sid input queue that was DEFERRED because
    the composer was busy (user mid-typing or Claude mid-generation) at write
    time. Once the composer frees up, this drains + delivers the batch without
    needing a fresh write. Idempotent — an empty or still-busy queue is a no-op
    / re-deferred. No-op under ``BOT_SQUAD_INPUT_MUX=0``. Self-contained; errors
    are swallowed so one bad queue never kills the sweep.
    """
    import os
    if os.environ.get("BOT_SQUAD_INPUT_MUX") == "0":
        return
    from bot_squad_worker import input_mux as _im
    try:
        _im.flush_pending(cfg.data_dir)
    except Exception:
        log.exception("input_flush_tick error")


def drift_check_tick(cfg: Config) -> None:
    """T-0149: per-project drift-enforcement pass.

    Injects a drift-check reminder into live, task-bound dev sessions that
    have gone too long without updating their ticket or whose recent activity
    is off-task (superpowers docs / premature automation). Disabled when
    ``BOT_SQUAD_DRIFT_MINUTES=0``. Per-project errors are caught and logged so
    one bad project never kills the sweep.
    """
    from bot_squad_worker import drift as _drift

    for slug in cfg.projects:
        try:
            _drift.drift_check(cfg, slug)
        except Exception:
            log.exception("drift_check_tick: unhandled error for project %s", slug)


def budding_check_tick(cfg: Config) -> None:
    """T-0932: per-project gradual-budding pass.

    SUGGESTS (never performs) a bud to a project's ROOT session when the
    energy ladder says the load has outgrown its current rung — too many
    queued user requests behind the task it is holding, or too many devs to
    steer without an operator. Triggers suggest, sessions decide (T-0929), so
    this tick's only side effect is one advisory line in a root pane, at most
    once per cooldown window and never into a pane a human is watching or
    typing in. Disabled by ``BOT_SQUAD_BUDDING=0``. Per-project errors are
    caught and logged so one bad project never kills the sweep.
    """
    from bot_squad_worker import budding as _budding

    for slug in cfg.projects:
        try:
            _budding.budding_check(cfg, slug)
        except Exception:
            log.exception("budding_check_tick: unhandled error for project %s", slug)


def voice_audio_gc_tick(cfg: Config) -> None:
    """T-0433 P3: per-project retention GC for voice-note audio blobs.

    Reaps ``feedback/_audio/`` blobs whose F-*.md was promoted/dismissed (triaged)
    and ages out abandoned orphans, so the audio store can't grow unbounded.
    Low-frequency + idempotent; per-project errors are caught so one bad project
    never kills the sweep.
    """
    from bot_squad_worker import voice_intake as _vi

    for slug in cfg.projects:
        try:
            _vi.gc_audio(cfg, slug)
        except Exception:
            log.exception("voice_audio_gc_tick: unhandled error for project %s", slug)


def task_lifecycle_tick(cfg: Config) -> None:
    """T-0589: stakeholder-task lifecycle notifications into the TG thread.

    Sweeps backlog frontmatter for provenance ``stakeholder:*`` tasks and posts
    one line per status transition into in_progress/totest/closed — batched per
    sweep, deduped via a sidecar, quiet-hours deferred (see
    ``task_chat.lifecycle_tick``). Per-project errors are contained inside the
    tick; this wrapper guards the scheduler thread.
    """
    from bot_squad_worker import task_chat as _task_chat

    try:
        _task_chat.lifecycle_tick(cfg)
    except Exception:
        log.exception("task_lifecycle_tick error")


def ticket_deadline_tick(cfg: Config) -> None:
    """T-0950: sweep blocked_on_user/to_accept/totest for tickets past their
    per-status deadline and page the stakeholder once per breach (see
    ``status_deadlines.deadline_check_tick``). Per-project errors are
    contained inside the tick; this wrapper guards the scheduler thread.
    """
    from bot_squad_worker import status_deadlines as _status_deadlines

    try:
        _status_deadlines.deadline_check_tick(cfg)
    except Exception:
        log.exception("ticket_deadline_tick error")


def outbound_drain_tick(cfg: Config) -> None:
    """T-0755: mirror delivered outbound messages into the conversation store.

    The transports write every send to a local spool the instant it lands (no
    network on the send path — that path must not slow down, and a logging
    failure must never break a delivery). This tick is the second half: it
    appends those records into ``conversations/<slug>/<gid>[/tN].jsonl`` through
    the API's token-gated append, so the file a human actually opens shows both
    halves of the dialogue instead of reading like an inbox.

    Cadence is 30s because the cost of lag here is an operator reading a
    *recent* window and seeing a gap that is only a drain delay — the exact
    false conclusion this ticket exists to prevent, in miniature. Idempotent
    (a line-cursor, never a re-read), self-bounded (``DRAIN_BATCH``), and
    non-raising; a stuck drain is loud in the log and in
    ``outbound_log.spool_health``, never silent.
    """
    from bot_squad_worker import outbound_log as _ob

    try:
        out = _ob.drain(cfg)
        if out.get("failed") or out.get("drops", {}).get("record"):
            log.warning("outbound_drain_tick: %s", out)
        elif out.get("mirrored"):
            log.info("outbound_drain_tick: mirrored %d outbound record(s)",
                     out["mirrored"])
    except Exception:
        log.exception("outbound_drain_tick error")


def outbound_liveness_tick(cfg: Config) -> None:
    """T-0759: is the outbound log still recording what we send?

    T-0755's own defect was a recording path that fell out of use unnoticed for
    a month; a fix that can decay the same way has only reset that clock. Since
    T-0746 the spool is also load-bearing for CORRECTNESS — ``echo_guard``
    rung 2 reads it, and it is the only rung that catches a copy-paste or
    hide-sender forward — so a silent decay re-attributes our own text to the
    stakeholder with no failing test and nothing announcing it.

    Cheap by construction: a directory scan and two mtimes, no network and no
    AI, and it announces only on a transition that has HELD (see
    ``outbound_liveness.PERSIST_S``). 5 minutes because the thing being watched
    decays over weeks — detection latency here is irrelevant next to the
    false-alarm cost of watching it twitch. Non-raising; the module already
    reports its own failure as BLIND rather than as health.
    """
    from bot_squad_worker import outbound_liveness as _ol

    try:
        _ol.tick(cfg)
    except Exception:
        log.exception("outbound_liveness_tick error")


def autopilot_tick(cfg: Config) -> None:
    """T-0153: per-project autopilot watchdog pass.

    Walks every active autopilot run, auto-ends ones whose duration has elapsed
    (notifying the stakeholder), and re-pings stalled targets (no commits /
    progress notes / activity for >= the configured stall threshold). Each
    autopilot self-throttles to its own ``watchdog_minutes`` cadence, so this
    can safely fire every 60s. Per-project errors are caught and logged so one
    bad project never kills the sweep.
    """
    from bot_squad_worker import autopilot as _ap

    for slug in cfg.projects:
        try:
            _ap.tick(cfg, slug)
        except Exception:
            log.exception("autopilot_tick: unhandled error for project %s", slug)


def drive_stop_tick(cfg: Config) -> None:
    """T-0800: per-project drive STOPPING-CONDITION pass — «stall alert /
    alert me when done».

    Announces, once per idle episode, that a project's drive has run out of
    work («ВСЁ СДЕЛАНО, ПРОВЕРЯЙ, МЫ ПРОСТАИВАЕМ») so the idle time becomes his
    decision rather than a silent gap. Opt-in per project: :func:`drive_stop.tick`
    reads ``on_stop`` from the pace config FIRST and returns before scanning the
    board when it is not ``alert``, so this costs one small JSON read per
    project per minute until he sets the mode. The alert itself is edge-
    triggered (fires on the transition INTO stopped, re-arms when the scope is
    non-empty again), so a 60s cadence cannot turn it into a repeating page.
    Per-project errors are caught inside the sweep. Kill switch:
    ``BOT_SQUAD_DRIVE_STOP_ALERT=0``.
    """
    from bot_squad_worker import drive_stop as _drive_stop

    try:
        _drive_stop.drive_stop_tick(cfg)
    except Exception:
        log.exception("drive_stop_tick error")


def constant_team_tick(cfg: Config) -> None:
    """T-0154: per-project constant-team maintenance pass.

    Keeps long-lived teams (initiatives with ``constant_team: true``) staffed:
    spawns a triage session when there is pending work and the team is below
    ``team_size``. Demand-driven — an idle queue spawns nothing. Per-project
    errors are caught and logged so one bad project never kills the sweep.
    """
    from bot_squad_worker import constant_teams as _ct

    for slug in cfg.projects:
        try:
            _ct.tick(cfg, slug)
        except Exception:
            log.exception("constant_team_tick: unhandled error for project %s", slug)


def tg_stall_tick(cfg: Config) -> None:
    """T-0155: escalate agents blocked on the stakeholder to TG.

    Fires every 60s. Each marker self-throttles via its ``since`` timestamp
    (only escalates past ``cfg.tg_stall_minutes``) and a one-shot ``escalated``
    flag, so this can safely run on a tight cadence without flooding. Disabled
    when ``tg_stall_minutes`` <= 0. Errors are caught so one bad sweep never
    kills the scheduler.
    """
    from bot_squad_worker import tg_stall as _tg_stall
    try:
        _tg_stall.tick(cfg)
    except Exception:
        log.exception("tg_stall_tick error")


def stall_sweep_tick(cfg: Config) -> None:
    """T-0696: sweep every live pane for a known stuck-interactive-TUI prompt
    (e.g. the Fable-5 usage-credits gate) and auto-answer it in the safe
    direction.

    Invisible to drift-check (ticket/commit timestamps) and idle-timeout
    (blocked on stdin before a first turn -- never idle by the jsonl/hook
    clock) -- this is the dedicated sweep for that stall class. Fires every
    60s; each marker self-throttles (retry cooldown + a capped attempt count
    before escalating instead of hammering the pane). No-op under
    BOT_SQUAD_STALL_SWEEP=0. Errors are caught so one bad sweep never kills
    the scheduler.
    """
    from bot_squad_worker import stall_sweep as _stall_sweep
    try:
        _stall_sweep.tick(cfg)
    except Exception:
        log.exception("stall_sweep_tick error")


def backoff_tick(cfg: Config) -> None:
    """WS-4 S2 (T-0249): run-survival parallelism-backoff governor.

    One AIMD step every 30s — depresses effective concurrency under Claude
    rate-limit / 5h-usage-limit pressure (``detector.session_pressure``) and
    ramps back toward the cap when clear. ``effective_limit`` is consulted at
    spawn admission (T-0250). No-op under ``BOT_SQUAD_BACKOFF=0``. The governor
    step never raises, but wrap it here too so one bad sweep never kills the
    scheduler.
    """
    from bot_squad_worker import backoff as _backoff
    try:
        _backoff.backoff_tick(cfg)
    except Exception:
        log.exception("backoff_tick error")


def recovery_tick(cfg: Config) -> None:
    """WS-4 S4 (T-0251): auto stall-recovery — dead/exited dev respawn-or-park.

    Default OFF (``BOT_SQUAD_RECOVERY=1`` to opt in). Only acts on dead-pane dev
    sessions whose task still needs work; respawn is bounded then parks +
    notifies the operator. Never touches live panes. Errors caught so one bad
    sweep never kills the scheduler.
    """
    from bot_squad_worker import recovery as _recovery
    try:
        _recovery.recovery_tick(cfg)
    except Exception:
        log.exception("recovery_tick error")


def park_tick(cfg: Config) -> None:
    """WS-4 S5 (T-0252): 5h-usage-limit park + retry.

    Default OFF (``BOT_SQUAD_PARK_RETRY=1`` to opt in). Suspends a session whose
    pane shows the 5h-limit prompt (after a debounce) to free its slot, then
    resumes it once the limit resets + a slot is free. Errors caught so one bad
    sweep never kills the scheduler.
    """
    from bot_squad_worker import park as _park
    try:
        _park.park_tick(cfg)
    except Exception:
        log.exception("park_tick error")


def oauth_refresh(cfg: Config) -> None:
    """Refresh Claude OAuth credentials. TG-ping on failure only.

    Calls refresh_oauth(cfg) from refresh_oauth.py and pings TG if it
    returns ok=False.
    """
    from bot_squad_worker.refresh_oauth import refresh_oauth as _refresh
    from bot_squad_worker.actions import _send_stakeholder_dm
    from bot_squad_worker import tg_topics as _tg_topics

    try:
        result = _refresh(cfg)
    except Exception as e:
        log.exception("oauth_refresh: unexpected error")
        result = {"ok": False, "action": "failed", "detail": str(e)}

    if not result.get("ok"):
        detail = result.get("detail", "unknown error")
        # An oauth-refresh failure is a P1 SYSTEM page — once creds expire every
        # session breaks. Route via the _send_stakeholder_dm SSOT (P2-08):
        # MAX-primary on this DPI-blocked host (a raw TG send was silently
        # dropped here), urgent so the quiet-hours gate can't drop it, with a
        # best-effort #team-queries group-record on the first project's chat.
        slug, project = next(iter(cfg.projects.items()), ("", None))
        _send_stakeholder_dm(
            cfg,
            message=f"❌ oauth_refresh FAILED: {detail}",
            sid="oauth_refresh",
            # T-0758 follow-up: deliberately NOT slug-tagged, unlike the
            # autopilot/telemetry twins. `slug` here is whichever project
            # happens to sort first — it is only a source of a chat to page
            # into. An OAuth failure breaks EVERY session on the install, so
            # `[bot-squad oauth_refresh]` would name a project that has no more
            # to do with it than the other one. Same call T-0724 made for
            # `autoupdate_apply` ("an apply failure is the install's, not any
            # session's"). Naming nobody is honest; naming the wrong one is not.
            urgent=True,
            tg_chat_id=getattr(project, "tg_chat", "") if project else "",
            tg_topic_id=_tg_topics.resolve(cfg, slug, "team_queries") if slug else None,
            group_record=bool(getattr(project, "tg_chat", "") if project else ""),
            # T-0799: URGENT class — once creds expire every session on the
            # install breaks. `route_slug` rather than `slug` for the reason the
            # comment above gives for not tagging one: `slug` here is whichever
            # project sorts first and is only a source of a chat, so it names
            # the map to read without claiming the failure is that project's.
            msg_type="oauth_expired",
            route_slug=slug,
        )
