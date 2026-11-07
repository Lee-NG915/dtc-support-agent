import { useEffect, useId, useMemo, useRef, useState } from "react";
import type { KeyboardEvent as ReactKeyboardEvent } from "react";
import { CalendarDays, Check, ChevronDown, ChevronLeft, ChevronRight } from "lucide-react";

function cx(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(" ");
}

export interface SelectOption {
  value: string;
  label: string;
  disabled?: boolean;
}

export interface SelectControlProps {
  value: string;
  onChange: (value: string) => void;
  options: readonly SelectOption[];
  ariaLabel?: string;
  disabled?: boolean;
  name?: string;
  placeholder?: string;
  className?: string;
  id?: string;
  ariaDescribedBy?: string;
}

/**
 * Select-only combobox. Deliberately avoids a native select element so the
 * trigger, popover, hover/selected states and focus ring match the staff
 * design system.
 * Focus stays on the trigger; the highlighted option is exposed through
 * aria-activedescendant.
 */
export function SelectControl({
  value,
  onChange,
  options,
  ariaLabel,
  disabled = false,
  name,
  placeholder,
  className,
  id,
  ariaDescribedBy,
}: SelectControlProps) {
  const reactId = useId();
  const listboxId = `${reactId}-listbox`;
  const optionId = (index: number) => `${reactId}-option-${index}`;
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const optionRefs = useRef<Array<HTMLDivElement | null>>([]);
  const [open, setOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(-1);
  const selectedIndex = options.findIndex((option) => option.value === value);
  const selectedOption = selectedIndex >= 0 ? options[selectedIndex] : undefined;

  useEffect(() => {
    if (!disabled) return;
    setOpen(false);
    setActiveIndex(-1);
  }, [disabled]);

  useEffect(() => {
    if (!open) return;
    function handlePointerDown(event: MouseEvent) {
      if (event.target instanceof Node && rootRef.current?.contains(event.target)) return;
      setOpen(false);
      setActiveIndex(-1);
    }
    function handleViewportChange() {
      setOpen(false);
      setActiveIndex(-1);
    }
    function handleScroll(event: Event) {
      // Scrolling the menu itself must not dismiss it.
      if (event.target instanceof Node && rootRef.current?.contains(event.target)) return;
      setOpen(false);
      setActiveIndex(-1);
    }
    document.addEventListener("mousedown", handlePointerDown);
    window.addEventListener("resize", handleViewportChange);
    window.addEventListener("scroll", handleScroll, true);
    return () => {
      document.removeEventListener("mousedown", handlePointerDown);
      window.removeEventListener("resize", handleViewportChange);
      window.removeEventListener("scroll", handleScroll, true);
    };
  }, [open]);

  useEffect(() => {
    if (!open || activeIndex < 0) return;
    optionRefs.current[activeIndex]?.scrollIntoView?.({ block: "nearest" });
  }, [activeIndex, open]);

  function openMenu() {
    const fallback = options.findIndex((option) => !option.disabled);
    const selectedIsUsable = selectedIndex >= 0 && options[selectedIndex]?.disabled !== true;
    setActiveIndex(selectedIsUsable ? selectedIndex : fallback);
    setOpen(true);
  }

  function closeMenu(focusTrigger = false) {
    setOpen(false);
    setActiveIndex(-1);
    if (focusTrigger) triggerRef.current?.focus();
  }

  function selectOption(option: SelectOption | undefined) {
    if (!option || option.disabled) return;
    if (option.value !== value) onChange(option.value);
    closeMenu(true);
  }

  function moveActive(direction: 1 | -1) {
    const enabled = options
      .map((option, index) => ({ option, index }))
      .filter((entry) => !entry.option.disabled);
    if (enabled.length === 0) return;
    const currentPosition = enabled.findIndex((entry) => entry.index === activeIndex);
    const nextPosition = currentPosition === -1
      ? (direction === 1 ? 0 : enabled.length - 1)
      : (currentPosition + direction + enabled.length) % enabled.length;
    const next = enabled[nextPosition];
    if (next) setActiveIndex(next.index);
  }

  function moveToEdge(edge: "first" | "last") {
    const enabled = options
      .map((option, index) => ({ option, index }))
      .filter((entry) => !entry.option.disabled);
    const target = edge === "first" ? enabled[0] : enabled[enabled.length - 1];
    if (target) setActiveIndex(target.index);
  }

  function handleTriggerKeyDown(event: ReactKeyboardEvent<HTMLButtonElement>) {
    if (disabled) return;
    if (!open) {
      if (event.key === "Enter" || event.key === " " ||
        event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        openMenu();
      }
      return;
    }
    switch (event.key) {
      case "ArrowDown":
        event.preventDefault();
        moveActive(1);
        break;
      case "ArrowUp":
        event.preventDefault();
        moveActive(-1);
        break;
      case "Home":
        event.preventDefault();
        moveToEdge("first");
        break;
      case "End":
        event.preventDefault();
        moveToEdge("last");
        break;
      case "Enter":
      case " ":
        event.preventDefault();
        selectOption(options[activeIndex]);
        break;
      case "Escape":
        event.preventDefault();
        closeMenu(true);
        break;
      case "Tab":
        closeMenu(false);
        break;
      default:
        break;
    }
  }

  const activeDescendant = open && activeIndex >= 0 && activeIndex < options.length
    ? optionId(activeIndex)
    : undefined;

  return (
    <div className={cx("fc-select", className)} ref={rootRef}>
      {name ? <input type="hidden" name={name} value={value} /> : null}
      <button
        ref={triggerRef}
        id={id}
        type="button"
        className="fc-trigger"
        role="combobox"
        aria-label={ariaLabel}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={open ? listboxId : undefined}
        aria-activedescendant={activeDescendant}
        aria-describedby={ariaDescribedBy}
        disabled={disabled}
        onClick={() => {
          if (open) closeMenu(false);
          else openMenu();
        }}
        onKeyDown={handleTriggerKeyDown}
      >
        <span className={cx("fc-trigger-label", !selectedOption && "fc-trigger-placeholder")}>
          {selectedOption ? selectedOption.label : placeholder ?? ""}
        </span>
        <ChevronDown className="fc-chevron" size={15} aria-hidden="true" />
      </button>
      {open ? (
        <div className="fc-popover">
          <div className="fc-menu" id={listboxId} role="listbox" aria-label={ariaLabel}>
            {options.length === 0 ? (
              <div className="fc-option fc-option-empty" role="option" aria-disabled="true">
                暂无可选项
              </div>
            ) : options.map((option, index) => {
              const isSelected = option.value === value;
              return (
                <div
                  key={`${option.value}:${index}`}
                  id={optionId(index)}
                  ref={(node) => {
                    optionRefs.current[index] = node;
                  }}
                  role="option"
                  aria-selected={isSelected}
                  aria-disabled={option.disabled ? true : undefined}
                  data-highlighted={index === activeIndex ? "true" : undefined}
                  className={cx("fc-option", option.disabled && "fc-option-disabled")}
                  // A future caller might still place this control inside a
                  // label. Keep focus, prevent native activation, and stop the
                  // event before a label can re-click the trigger.
                  onPointerDown={(event) => event.stopPropagation()}
                  onMouseDown={(event) => {
                    event.preventDefault();
                    event.stopPropagation();
                  }}
                  onMouseEnter={() => {
                    if (!option.disabled) setActiveIndex(index);
                  }}
                  onClick={(event) => {
                    event.preventDefault();
                    event.stopPropagation();
                    selectOption(option);
                  }}
                >
                  <span>{option.label}</span>
                  {isSelected ? <Check size={14} aria-hidden="true" /> : null}
                </div>
              );
            })}
          </div>
        </div>
      ) : null}
    </div>
  );
}

export type DatePickerMode = "date" | "datetime-local";

export interface DatePickerProps {
  /** `YYYY-MM-DD` for date mode, `YYYY-MM-DDTHH:mm` for datetime-local mode. */
  value: string;
  mode?: DatePickerMode;
  onChange: (value: string) => void;
  ariaLabel?: string;
  disabled?: boolean;
  name?: string;
  placeholder?: string;
  className?: string;
  id?: string;
  ariaDescribedBy?: string;
}

interface ParsedDateValue {
  year: number;
  month: number;
  day: number;
  time: string | null;
}

const DATE_VALUE_RE = /^(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}):(\d{2}))?$/;
const TIME_VALUE_RE = /^([01]\d|2[0-3]):[0-5]\d$/;

// The staff workbench is Chinese-only. Pin the calendar locale so a browser
// running with an English runtime locale still renders Chinese labels.
const STAFF_DATE_LOCALE = "zh-CN";
const DATE_DISPLAY_FORMATTER = new Intl.DateTimeFormat(STAFF_DATE_LOCALE, {
  year: "numeric",
  month: "short",
  day: "numeric",
});
const MONTH_DISPLAY_FORMATTER = new Intl.DateTimeFormat(STAFF_DATE_LOCALE, {
  year: "numeric",
  month: "long",
});
const WEEKDAY_FORMATTER = new Intl.DateTimeFormat(STAFF_DATE_LOCALE, { weekday: "short" });

// 2024-01-01 is a Monday, so this yields Monday-first week headings.
const WEEKDAY_LABELS = Array.from({ length: 7 }, (_, index) =>
  WEEKDAY_FORMATTER.format(new Date(2024, 0, 1 + index)));

function pad2(value: number): string {
  return String(value).padStart(2, "0");
}

function formatDateKey(date: Date): string {
  return `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}`;
}

function startOfDay(date: Date): Date {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate());
}

function addDays(date: Date, delta: number): Date {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate() + delta);
}

function addMonthsClamped(date: Date, delta: number): Date {
  const target = new Date(date.getFullYear(), date.getMonth() + delta, 1);
  const lastDay = new Date(target.getFullYear(), target.getMonth() + 1, 0).getDate();
  return new Date(target.getFullYear(), target.getMonth(), Math.min(date.getDate(), lastDay));
}

function sameDay(left: Date, right: Date): boolean {
  return left.getFullYear() === right.getFullYear() &&
    left.getMonth() === right.getMonth() &&
    left.getDate() === right.getDate();
}

function parseDateValue(value: string): ParsedDateValue | null {
  const match = DATE_VALUE_RE.exec(value);
  if (!match) return null;
  const yearText = match[1];
  const monthText = match[2];
  const dayText = match[3];
  if (yearText === undefined || monthText === undefined || dayText === undefined) return null;
  const year = Number(yearText);
  const month = Number(monthText);
  const day = Number(dayText);
  if (month < 1 || month > 12 || day < 1 || day > 31) return null;
  const date = new Date(year, month - 1, day);
  if (date.getFullYear() !== year || date.getMonth() !== month - 1 || date.getDate() !== day) {
    return null;
  }
  const hourText = match[4];
  const minuteText = match[5];
  let time: string | null = null;
  if (hourText !== undefined && minuteText !== undefined) {
    if (Number(hourText) > 23 || Number(minuteText) > 59) return null;
    time = `${hourText}:${minuteText}`;
  }
  return { year, month, day, time };
}

function dateFromParsed(parsed: ParsedDateValue): Date {
  return new Date(parsed.year, parsed.month - 1, parsed.day);
}

/**
 * Custom date / datetime-local picker. The trigger and calendar never use
 * native date, datetime-local or time inputs, so browser popovers cannot leak
 * into the staff workbench. Values stay in the existing local ISO shapes.
 */
export function DatePicker({
  value,
  mode = "date",
  onChange,
  ariaLabel,
  disabled = false,
  name,
  placeholder,
  className,
  id,
  ariaDescribedBy,
}: DatePickerProps) {
  const label = ariaLabel ?? (mode === "date" ? "选择日期" : "选择日期时间");
  const reactId = useId();
  const dialogId = `${reactId}-dialog`;
  const triggerRef = useRef<HTMLButtonElement>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  const dayRefs = useRef(new Map<string, HTMLButtonElement>());

  const parsed = useMemo(() => parseDateValue(value), [value]);
  const parsedTime = parsed?.time ?? "00:00";
  const [open, setOpen] = useState(false);
  const [timeText, setTimeText] = useState(parsedTime);
  const [view, setView] = useState(() => {
    const base = parsed ? dateFromParsed(parsed) : new Date();
    return { year: base.getFullYear(), month: base.getMonth() };
  });
  const [activeDate, setActiveDate] = useState<Date>(() =>
    startOfDay(parsed ? dateFromParsed(parsed) : new Date()));

  useEffect(() => {
    setTimeText(parsedTime);
  }, [parsedTime]);

  useEffect(() => {
    if (!disabled) return;
    setOpen(false);
  }, [disabled]);

  useEffect(() => {
    if (!open) return;
    function handlePointerDown(event: MouseEvent) {
      if (event.target instanceof Node && rootRef.current?.contains(event.target)) return;
      setOpen(false);
    }
    function handleViewportChange() {
      setOpen(false);
    }
    function handleScroll(event: Event) {
      if (event.target instanceof Node && rootRef.current?.contains(event.target)) return;
      setOpen(false);
    }
    document.addEventListener("mousedown", handlePointerDown);
    window.addEventListener("resize", handleViewportChange);
    window.addEventListener("scroll", handleScroll, true);
    return () => {
      document.removeEventListener("mousedown", handlePointerDown);
      window.removeEventListener("resize", handleViewportChange);
      window.removeEventListener("scroll", handleScroll, true);
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    dayRefs.current.get(formatDateKey(activeDate))?.focus();
  }, [activeDate, open]);

  function openPicker() {
    if (disabled) return;
    const base = parsed ? dateFromParsed(parsed) : new Date();
    setView({ year: base.getFullYear(), month: base.getMonth() });
    setActiveDate(startOfDay(base));
    setTimeText(parsed?.time ?? "00:00");
    setOpen(true);
  }

  function closePicker(focusTrigger = false) {
    setOpen(false);
    if (focusTrigger) triggerRef.current?.focus();
  }

  function chooseDate(day: Date) {
    const key = formatDateKey(day);
    setView({ year: day.getFullYear(), month: day.getMonth() });
    setActiveDate(startOfDay(day));
    if (mode === "date") {
      if (key !== value) onChange(key);
      closePicker(true);
      return;
    }
    // datetime-local: keep the popover open so the time can be completed.
    const nextTime = TIME_VALUE_RE.test(timeText) ? timeText : "00:00";
    if (nextTime !== timeText) setTimeText(nextTime);
    const nextValue = `${key}T${nextTime}`;
    if (nextValue !== value) onChange(nextValue);
  }

  function chooseToday() {
    chooseDate(startOfDay(new Date()));
  }

  function clearValue() {
    setTimeText("00:00");
    if (value !== "") onChange("");
    closePicker(true);
  }

  function changeTime(next: string) {
    setTimeText(next);
    if (!parsed || !TIME_VALUE_RE.test(next)) return;
    const nextValue = `${formatDateKey(dateFromParsed(parsed))}T${next}`;
    if (nextValue !== value) onChange(nextValue);
  }

  function moveMonth(delta: number) {
    const next = addMonthsClamped(activeDate, delta);
    setActiveDate(next);
    setView({ year: next.getFullYear(), month: next.getMonth() });
  }

  function handleDayKeyDown(event: ReactKeyboardEvent<HTMLButtonElement>, day: Date) {
    let target: Date;
    switch (event.key) {
      case "ArrowLeft":
        target = addDays(day, -1);
        break;
      case "ArrowRight":
        target = addDays(day, 1);
        break;
      case "ArrowUp":
        target = addDays(day, -7);
        break;
      case "ArrowDown":
        target = addDays(day, 7);
        break;
      case "Home":
        target = addDays(day, -((day.getDay() + 6) % 7));
        break;
      case "End":
        target = addDays(day, 6 - ((day.getDay() + 6) % 7));
        break;
      case "PageUp":
        target = addMonthsClamped(day, -1);
        break;
      case "PageDown":
        target = addMonthsClamped(day, 1);
        break;
      case "Enter":
      case " ":
        event.preventDefault();
        chooseDate(day);
        return;
      case "Escape":
        event.preventDefault();
        closePicker(true);
        return;
      default:
        return;
    }
    event.preventDefault();
    const nextDay = startOfDay(target);
    setActiveDate(nextDay);
    setView({ year: nextDay.getFullYear(), month: nextDay.getMonth() });
  }

  function handleTimeKeyDown(event: ReactKeyboardEvent<HTMLInputElement>) {
    if (event.key !== "Escape") return;
    event.preventDefault();
    closePicker(true);
  }

  const viewFirst = new Date(view.year, view.month, 1);
  const gridStart = addDays(viewFirst, -((viewFirst.getDay() + 6) % 7));
  const days = Array.from({ length: 42 }, (_, index) => addDays(gridStart, index));
  const monthLabel = MONTH_DISPLAY_FORMATTER.format(viewFirst);
  const selectedKey = parsed ? formatDateKey(dateFromParsed(parsed)) : null;
  const today = startOfDay(new Date());
  const displayText = parsed
    ? mode === "datetime-local" && parsed.time
      ? `${DATE_DISPLAY_FORMATTER.format(dateFromParsed(parsed))} ${parsed.time}`
      : DATE_DISPLAY_FORMATTER.format(dateFromParsed(parsed))
    : placeholder ?? (mode === "date" ? "选择日期" : "选择日期时间");

  return (
    <div className={cx("fc-date", className)} ref={rootRef}>
      {name ? <input type="hidden" name={name} value={value} /> : null}
      <button
        ref={triggerRef}
        id={id}
        type="button"
        className="fc-trigger"
        aria-label={label}
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-controls={open ? dialogId : undefined}
        aria-describedby={ariaDescribedBy}
        disabled={disabled}
        onClick={() => {
          if (open) closePicker(false);
          else openPicker();
        }}
        onKeyDown={(event) => {
          if (!open) {
            if (event.key === "Enter" || event.key === " " || event.key === "ArrowDown") {
              event.preventDefault();
              openPicker();
            }
            return;
          }
          if (event.key === "Escape") {
            event.preventDefault();
            closePicker(true);
          }
        }}
      >
        <span className={cx("fc-trigger-label", !parsed && "fc-trigger-placeholder")}>
          {displayText}
        </span>
        <CalendarDays className="fc-trigger-icon" size={15} aria-hidden="true" />
      </button>
      {open ? (
        <div
          className="fc-popover fc-calendar"
          id={dialogId}
          role="dialog"
          aria-label={`${label} 日历`}
        >
          <div className="fc-calendar-head">
            <button
              type="button"
              className="staff-icon-button"
              aria-label="上个月"
              onClick={() => moveMonth(-1)}
            >
              <ChevronLeft size={16} aria-hidden="true" />
            </button>
            <strong className="fc-calendar-title" aria-live="polite">{monthLabel}</strong>
            <button
              type="button"
              className="staff-icon-button"
              aria-label="下个月"
              onClick={() => moveMonth(1)}
            >
              <ChevronRight size={16} aria-hidden="true" />
            </button>
          </div>
          <div className="fc-calendar-weekdays" aria-hidden="true">
            {WEEKDAY_LABELS.map((weekday) => <span key={weekday}>{weekday}</span>)}
          </div>
          <div className="fc-calendar-grid" role="group" aria-label={monthLabel}>
            {days.map((day) => {
              const key = formatDateKey(day);
              const isSelected = key === selectedKey;
              const isOutside = day.getMonth() !== view.month || day.getFullYear() !== view.year;
              return (
                <button
                  key={key}
                  ref={(node) => {
                    if (node) dayRefs.current.set(key, node);
                    else dayRefs.current.delete(key);
                  }}
                  type="button"
                  className={cx("fc-day", isOutside && "fc-day-outside")}
                  tabIndex={sameDay(day, activeDate) ? 0 : -1}
                  aria-label={DATE_DISPLAY_FORMATTER.format(day)}
                  aria-pressed={isSelected}
                  aria-current={sameDay(day, today) ? "date" : undefined}
                  onClick={() => chooseDate(day)}
                  onKeyDown={(event) => handleDayKeyDown(event, day)}
                >
                  {day.getDate()}
                </button>
              );
            })}
          </div>
          {mode === "datetime-local" ? (
            <label className="fc-time-field">
              <span className="fc-time-label">时间</span>
              <input
                type="text"
                className="fc-time-input"
                inputMode="numeric"
                autoComplete="off"
                value={timeText}
                placeholder="HH:mm"
                maxLength={5}
                aria-label={`${label} 时间`}
                aria-invalid={!TIME_VALUE_RE.test(timeText)}
                disabled={!parsed}
                onChange={(event) => changeTime(event.target.value)}
                onKeyDown={handleTimeKeyDown}
              />
              <span className="fc-time-hint">HH:mm</span>
            </label>
          ) : null}
          <div className="fc-calendar-actions">
            <button type="button" className="staff-button staff-button-quiet" onClick={chooseToday}>
              今天
            </button>
            <button type="button" className="staff-button staff-button-quiet" onClick={clearValue}>
              清除
            </button>
          </div>
        </div>
      ) : null}
    </div>
  );
}
