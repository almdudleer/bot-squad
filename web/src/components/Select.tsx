/**
 * Shared dropdown — replaces every native <select> in the app (T-0102).
 *
 * Props mirror the native essentials (value/options/onChange/disabled/
 * placeholder/id/title/className). The dropdown supports two option
 * kinds:
 *
 *   - value option: { value, label, disabled?, hint? }
 *   - action item : { action: true, key, label, onSelect, disabled? }
 *
 * Action items don't carry a value — selecting one fires `onSelect` and
 * closes the panel without calling `onChange`. They render under a
 * divider at the bottom of the list (used by T-0101's "Start new TL").
 *
 * Keyboard: ArrowDown/Up navigate; Enter/Space select highlighted item;
 * Escape closes; Tab closes and moves focus naturally; Home/End jump.
 * ARIA: trigger is a combobox with aria-haspopup=listbox + aria-expanded
 * + aria-activedescendant; the panel is role=listbox; each item is
 * role=option with aria-selected.
 */
import {
  CSSProperties,
  KeyboardEvent as ReactKeyboardEvent,
  ReactNode,
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
} from "react";

export interface SelectValueOption {
  value: string;
  label: ReactNode;
  disabled?: boolean;
  hint?: ReactNode;
}

export interface SelectActionItem {
  action: true;
  key: string;
  label: ReactNode;
  onSelect: () => void;
  disabled?: boolean;
}

export type SelectOption = SelectValueOption | SelectActionItem;

export function isActionItem(o: SelectOption): o is SelectActionItem {
  return (o as SelectActionItem).action === true;
}

/**
 * Find the next non-disabled index when stepping by `direction`. Wraps
 * around. Returns -1 if every option is disabled (or the list is empty).
 *
 * `from` is the current cursor; the search starts at `from + direction`.
 * To start "above the list" use from = -1 with direction = 1; to start
 * "below the list" use from = options.length with direction = -1.
 */
export function nextEnabledIndex(
  options: SelectOption[],
  from: number,
  direction: 1 | -1,
): number {
  const n = options.length;
  if (n === 0) return -1;
  let i = from;
  for (let step = 0; step < n; step++) {
    i = (i + direction + n) % n;
    if (!options[i].disabled) return i;
  }
  return -1;
}

/**
 * Index of the option whose value matches `value`. Returns -1 if no
 * value option matches (or `value` corresponds to no option at all —
 * action items don't carry a value).
 */
export function findSelectedIndex(options: SelectOption[], value: string): number {
  return options.findIndex((o) => !isActionItem(o) && o.value === value);
}

export interface SelectProps {
  value: string;
  options: SelectOption[];
  onChange: (value: string) => void;
  placeholder?: string;
  disabled?: boolean;
  id?: string;
  title?: string;
  className?: string;
  style?: CSSProperties;
  autoFocus?: boolean;
  ariaLabel?: string;
  // Override the closed-state label. Receives the currently selected
  // value option (or null when nothing matches — e.g. value === "").
  // Useful when callers want to render the bound entity differently
  // from how it appears in the dropdown list.
  renderValue?: (selected: SelectValueOption | null) => ReactNode;
}

export function Select(props: SelectProps) {
  const {
    value,
    options,
    onChange,
    placeholder,
    disabled,
    id,
    title,
    className,
    style,
    autoFocus,
    ariaLabel,
    renderValue,
  } = props;

  const reactId = useId();
  const listboxId = `${id ?? `select-${reactId}`}-listbox`;
  const itemId = useCallback(
    (i: number) => `${id ?? `select-${reactId}`}-opt-${i}`,
    [id, reactId],
  );

  const [open, setOpen] = useState(false);
  const [highlight, setHighlight] = useState<number>(() => {
    const sel = findSelectedIndex(options, value);
    if (sel >= 0 && !options[sel].disabled) return sel;
    return nextEnabledIndex(options, -1, 1);
  });
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const itemsRef = useRef<Array<HTMLButtonElement | null>>([]);

  const selectedOption =
    (options.find(
      (o): o is SelectValueOption => !isActionItem(o) && o.value === value,
    ) as SelectValueOption | undefined) ?? null;

  useEffect(() => {
    if (!open) return;
    function onDocPointer(e: MouseEvent) {
      if (!rootRef.current) return;
      if (!rootRef.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", onDocPointer);
    return () => document.removeEventListener("mousedown", onDocPointer);
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const el = itemsRef.current[highlight];
    if (el) el.scrollIntoView({ block: "nearest" });
  }, [open, highlight]);

  const openPanel = useCallback(
    (initialHighlight?: number) => {
      if (disabled) return;
      setOpen(true);
      if (initialHighlight !== undefined) {
        setHighlight(initialHighlight);
        return;
      }
      const sel = findSelectedIndex(options, value);
      if (sel >= 0 && !options[sel].disabled) {
        setHighlight(sel);
      } else {
        setHighlight(nextEnabledIndex(options, -1, 1));
      }
    },
    [disabled, options, value],
  );

  const closePanel = useCallback((focusTrigger = true) => {
    setOpen(false);
    if (focusTrigger) triggerRef.current?.focus();
  }, []);

  const selectIndex = useCallback(
    (i: number) => {
      const opt = options[i];
      if (!opt || opt.disabled) return;
      if (isActionItem(opt)) {
        opt.onSelect();
      } else {
        onChange(opt.value);
      }
      closePanel();
    },
    [options, onChange, closePanel],
  );

  function onTriggerKeyDown(e: ReactKeyboardEvent<HTMLButtonElement>) {
    if (!open) {
      if (e.key === "ArrowDown" || e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        openPanel();
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        openPanel(nextEnabledIndex(options, 0, -1));
        return;
      }
      return;
    }
    if (e.key === "Escape") {
      e.preventDefault();
      closePanel();
      return;
    }
    if (e.key === "Tab") {
      setOpen(false);
      return;
    }
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setHighlight((i) => nextEnabledIndex(options, i, 1));
      return;
    }
    if (e.key === "ArrowUp") {
      e.preventDefault();
      setHighlight((i) => nextEnabledIndex(options, i, -1));
      return;
    }
    if (e.key === "Home") {
      e.preventDefault();
      setHighlight(nextEnabledIndex(options, -1, 1));
      return;
    }
    if (e.key === "End") {
      e.preventDefault();
      setHighlight(nextEnabledIndex(options, 0, -1));
      return;
    }
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      if (highlight >= 0) selectIndex(highlight);
      return;
    }
  }

  const displayLabel = renderValue
    ? renderValue(selectedOption)
    : selectedOption?.label ?? (
        <span className="mc-select-placeholder">{placeholder ?? ""}</span>
      );

  // Split options visually: actions render under a divider at the bottom.
  const actionItems: Array<{ opt: SelectActionItem; index: number }> = [];
  const valueItems: Array<{ opt: SelectValueOption; index: number }> = [];
  options.forEach((o, i) => {
    if (isActionItem(o)) actionItems.push({ opt: o, index: i });
    else valueItems.push({ opt: o, index: i });
  });

  function renderOption(o: SelectOption, i: number) {
    const isAction = isActionItem(o);
    const isSelected = !isAction && o.value === value;
    const isHighlighted = i === highlight;
    return (
      <button
        key={isAction ? `act-${o.key}` : `val-${(o as SelectValueOption).value}-${i}`}
        type="button"
        role="option"
        id={itemId(i)}
        aria-selected={isSelected}
        aria-disabled={o.disabled || undefined}
        disabled={o.disabled}
        tabIndex={-1}
        ref={(el) => {
          itemsRef.current[i] = el;
        }}
        className={
          "mc-select-option" +
          (isHighlighted ? " mc-select-option-highlight" : "") +
          (isSelected ? " mc-select-option-selected" : "") +
          (isAction ? " mc-select-option-action" : "")
        }
        onMouseEnter={() => {
          if (!o.disabled) setHighlight(i);
        }}
        onMouseDown={(e) => {
          // Prevent focus shift from the trigger so the click handler
          // fires cleanly and our closePanel() focus restore is sane.
          e.preventDefault();
        }}
        onClick={() => selectIndex(i)}
      >
        <span className="mc-select-option-label">{o.label}</span>
        {!isAction && (o as SelectValueOption).hint != null && (
          <span className="mc-select-option-hint">{(o as SelectValueOption).hint}</span>
        )}
      </button>
    );
  }

  return (
    <div className="mc-select" ref={rootRef}>
      <button
        ref={triggerRef}
        id={id}
        type="button"
        role="combobox"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={listboxId}
        aria-activedescendant={open && highlight >= 0 ? itemId(highlight) : undefined}
        aria-label={ariaLabel}
        disabled={disabled}
        title={title}
        className={"mc-select-trigger" + (className ? ` ${className}` : "")}
        style={style}
        autoFocus={autoFocus}
        onClick={() => (open ? closePanel(false) : openPanel())}
        onKeyDown={onTriggerKeyDown}
      >
        <span className="mc-select-value">{displayLabel}</span>
        <span className="mc-select-caret" aria-hidden="true">▾</span>
      </button>

      {open && (
        <div id={listboxId} role="listbox" className="mc-select-panel">
          {valueItems.length === 0 && actionItems.length === 0 && (
            <div className="mc-select-empty">(no options)</div>
          )}
          {valueItems.map(({ opt, index }) => renderOption(opt, index))}
          {actionItems.length > 0 && valueItems.length > 0 && (
            <div className="mc-select-divider" aria-hidden="true" />
          )}
          {actionItems.map(({ opt, index }) => renderOption(opt, index))}
        </div>
      )}
    </div>
  );
}
