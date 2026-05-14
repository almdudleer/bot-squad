import { useEffect, useLayoutEffect, useState } from "react";

import { useOnboardingStep } from "./useOnboardingStep";
import { OnboardingStepId } from "./stepIds";

type Placement = "top" | "bottom" | "left" | "right";

export type CoachmarkProps = {
  stepId: OnboardingStepId;
  title: string;
  body: React.ReactNode;
  // Optional. When omitted, renders as a centered modal-style card (suitable
  // for srv.intro and any beat that doesn't have a single DOM target).
  anchorSelector?: string;
  placement?: Placement;
};

const CARD_WIDTH = 320;
const GAP = 12;

type Rect = { top: number; left: number; width: number; height: number };

function getRect(selector: string): Rect | null {
  const el = document.querySelector(selector);
  if (!el) return null;
  const r = el.getBoundingClientRect();
  return { top: r.top, left: r.left, width: r.width, height: r.height };
}

function cardPosition(anchor: Rect, placement: Placement): { top: number; left: number } {
  switch (placement) {
    case "top":
      return { top: anchor.top - GAP, left: anchor.left + anchor.width / 2 - CARD_WIDTH / 2 };
    case "left":
      return { top: anchor.top + anchor.height / 2, left: anchor.left - CARD_WIDTH - GAP };
    case "right":
      return { top: anchor.top + anchor.height / 2, left: anchor.left + anchor.width + GAP };
    case "bottom":
    default:
      return { top: anchor.top + anchor.height + GAP, left: anchor.left + anchor.width / 2 - CARD_WIDTH / 2 };
  }
}

export function Coachmark({
  stepId,
  title,
  body,
  anchorSelector,
  placement = "bottom",
}: CoachmarkProps) {
  const step = useOnboardingStep(stepId);
  const [anchor, setAnchor] = useState<Rect | null>(null);

  useLayoutEffect(() => {
    if (!step.visible || !anchorSelector) return;
    let raf = 0;
    const update = () => {
      raf = requestAnimationFrame(() => setAnchor(getRect(anchorSelector)));
    };
    update();
    window.addEventListener("resize", update);
    window.addEventListener("scroll", update, true);
    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener("resize", update);
      window.removeEventListener("scroll", update, true);
    };
  }, [step.visible, anchorSelector]);

  useEffect(() => {
    if (!step.visible) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") step.dismiss();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [step.visible, step.dismiss]);

  if (!step.visible) return null;

  // No anchor selector: centered modal. With selector: position next to the
  // element if found, else fall back to centered (so a missing anchor never
  // strands a coachmark off-screen).
  const positioned = anchorSelector && anchor ? cardPosition(anchor, placement) : null;
  const cardStyle: React.CSSProperties = positioned
    ? { position: "fixed", top: positioned.top, left: positioned.left, width: CARD_WIDTH }
    : {
        position: "fixed",
        top: "50%",
        left: "50%",
        transform: "translate(-50%, -50%)",
        width: CARD_WIDTH,
      };

  return (
    <div className="bs-coachmark-root" role="dialog" aria-label={title}>
      <div className="bs-coachmark-backdrop" onClick={step.dismiss} />
      <div className="bs-coachmark-card card shadow" style={cardStyle}>
        <div className="card-body">
          <div className="card-title fw-semibold mb-2">{title}</div>
          <div className="card-text small mb-3">{body}</div>
          <div className="d-flex justify-content-between align-items-center">
            <button
              type="button"
              className="btn btn-link btn-sm p-0"
              onClick={step.skipAll}
            >
              Skip onboarding
            </button>
            <button
              type="button"
              className="btn btn-primary btn-sm"
              onClick={step.dismiss}
            >
              Got it
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
