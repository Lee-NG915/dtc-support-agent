// @vitest-environment jsdom
import { act, useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { DatePicker, SelectControl } from "./FormControls";
import type { DatePickerMode, SelectOption } from "./FormControls";
import { flushMicrotasks, renderUi, setControlValue } from "./testSupport";

const DATE_DISPLAY = new Intl.DateTimeFormat("zh-CN", {
  year: "numeric",
  month: "short",
  day: "numeric",
});
const MONTH_DISPLAY = new Intl.DateTimeFormat("zh-CN", {
  year: "numeric",
  month: "long",
});

const SELECT_OPTIONS: SelectOption[] = [
  { value: "a", label: "选项 A" },
  { value: "b", label: "选项 B" },
  { value: "c", label: "选项 C" },
];

function need<T extends Element>(value: T | null | undefined, label: string): T {
  if (value === null || value === undefined) throw new Error(`missing: ${label}`);
  return value;
}

function formatDate(date: Date): string {
  return DATE_DISPLAY.format(date);
}

function monthLabel(date: Date): string {
  return MONTH_DISPLAY.format(date);
}

function pad2(value: number): string {
  return String(value).padStart(2, "0");
}

function dateKey(date: Date): string {
  return `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}`;
}

function todayKey(): string {
  return dateKey(new Date());
}

function mondayOf(date: Date): Date {
  const offset = (date.getDay() + 6) % 7;
  return new Date(date.getFullYear(), date.getMonth(), date.getDate() - offset);
}

async function clickElement(element: Element): Promise<void> {
  await act(async () => {
    (element as HTMLElement).click();
    await flushMicrotasks();
  });
}

async function pressKey(element: Element, key: string): Promise<void> {
  await act(async () => {
    element.dispatchEvent(new KeyboardEvent("keydown", {
      key,
      bubbles: true,
      cancelable: true,
    }));
    await flushMicrotasks();
  });
}

async function clickOutside(): Promise<void> {
  await act(async () => {
    document.body.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
    await flushMicrotasks();
  });
}

function activeElement(): HTMLElement {
  return need(
    document.activeElement instanceof HTMLElement ? document.activeElement : null,
    "active element",
  );
}

function byLabel(container: HTMLElement, selector: string, label: string): HTMLElement {
  return need<HTMLElement>(
    container.querySelector<HTMLElement>(`${selector}[aria-label="${label}"]`),
    `${selector}:${label}`,
  );
}

function inputValue(container: HTMLElement, selector: string): string {
  return need(container.querySelector<HTMLInputElement>(selector), selector).value;
}

function optionsOf(container: HTMLElement): HTMLElement[] {
  return [...container.querySelectorAll<HTMLElement>('[role="option"]')];
}

function optionNamed(container: HTMLElement, label: string): HTMLElement {
  return need(
    optionsOf(container).find((option) => option.textContent?.includes(label)),
    `option ${label}`,
  );
}

function highlightedOption(trigger: HTMLElement): HTMLElement {
  const id = trigger.getAttribute("aria-activedescendant");
  return need(id ? document.getElementById(id) : null, "highlighted option");
}

function dayButton(container: HTMLElement, date: Date): HTMLButtonElement {
  return need(
    container.querySelector(`.fc-day[aria-label="${formatDate(date)}"]`),
    `day ${formatDate(date)}`,
  ) as HTMLButtonElement;
}

function buttonWithText(container: HTMLElement, text: string): HTMLButtonElement {
  return need(
    [...container.querySelectorAll("button")].find((button) => button.textContent?.includes(text)),
    `button ${text}`,
  );
}

function SelectHarness({
  initial = "b",
  disabled = false,
  withName = true,
  options = SELECT_OPTIONS,
  onChange,
}: {
  initial?: string;
  disabled?: boolean;
  withName?: boolean;
  options?: SelectOption[];
  onChange?: (value: string) => void;
}) {
  const [value, setValue] = useState(initial);
  return (
    <div>
      <SelectControl
        ariaLabel="测试选择"
        value={value}
        onChange={(next) => {
          setValue(next);
          onChange?.(next);
        }}
        options={options}
        disabled={disabled}
        name={withName ? "choice" : undefined}
        placeholder="请选择"
      />
      <output data-testid="value">{value}</output>
    </div>
  );
}

function DateHarness({
  initial = "2026-11-06",
  mode = "date",
  disabled = false,
  onChange,
}: {
  initial?: string;
  mode?: DatePickerMode;
  disabled?: boolean;
  onChange?: (value: string) => void;
}) {
  const [value, setValue] = useState(initial);
  return (
    <div>
      <DatePicker
        ariaLabel="测试日期"
        value={value}
        mode={mode}
        disabled={disabled}
        name="picked_date"
        placeholder="选择日期"
        onChange={(next) => {
          setValue(next);
          onChange?.(next);
        }}
      />
      <output data-testid="value">{value}</output>
    </div>
  );
}

afterEach(() => {
  document.body.innerHTML = "";
});

describe("SelectControl", () => {
  it("opens, exposes selected/highlighted aria state, selects with the mouse, and submits a hidden value", async () => {
    const onChange = vi.fn();
    const { container, unmount } = await renderUi(<SelectHarness onChange={onChange} />);

    const trigger = byLabel(container, "button", "测试选择") as HTMLButtonElement;
    expect(trigger.getAttribute("role")).toBe("combobox");
    expect(trigger.getAttribute("aria-expanded")).toBe("false");
    expect(trigger.textContent).toContain("选项 B");
    expect(container.querySelector('[role="listbox"]')).toBeNull();
    expect(inputValue(container, 'input[type="hidden"][name="choice"]')).toBe("b");

    await clickElement(trigger);
    const listbox = need(container.querySelector('[role="listbox"]'), "listbox") as HTMLElement;
    expect(trigger.getAttribute("aria-expanded")).toBe("true");
    expect(trigger.getAttribute("aria-controls")).toBe(listbox.id);
    expect(optionsOf(container)).toHaveLength(3);

    const selected = need(
      optionsOf(container).find((option) => option.getAttribute("aria-selected") === "true"),
      "selected option",
    );
    expect(selected.textContent).toContain("选项 B");
    expect(selected.getAttribute("data-highlighted")).toBe("true");
    expect(trigger.getAttribute("aria-activedescendant")).toBe(selected.id);
    expect(highlightedOption(trigger)).toBe(selected);

    await clickElement(optionNamed(container, "选项 C"));
    expect(onChange).toHaveBeenCalledWith("c");
    expect(container.querySelector('[role="listbox"]')).toBeNull();
    expect(trigger.getAttribute("aria-expanded")).toBe("false");
    expect(trigger.getAttribute("aria-activedescendant")).toBeNull();
    expect(trigger.textContent).toContain("选项 C");
    expect(document.activeElement).toBe(trigger);
    expect(inputValue(container, 'input[type="hidden"][name="choice"]')).toBe("c");
    expect(container.querySelector("select")).toBeNull();

    await unmount();
  });

  it("supports ArrowUp/ArrowDown/Home/End/Enter/Escape without moving focus off the trigger", async () => {
    const onChange = vi.fn();
    const { container, unmount } = await renderUi(
      <SelectHarness initial="a" onChange={onChange} />,
    );
    const trigger = byLabel(container, "button", "测试选择") as HTMLButtonElement;

    await pressKey(trigger, "ArrowDown");
    expect(container.querySelector('[role="listbox"]')).not.toBeNull();
    expect(highlightedOption(trigger).textContent).toContain("选项 A");

    await pressKey(trigger, "ArrowDown");
    expect(highlightedOption(trigger).textContent).toContain("选项 B");

    await pressKey(trigger, "End");
    expect(highlightedOption(trigger).textContent).toContain("选项 C");

    await pressKey(trigger, "Home");
    expect(highlightedOption(trigger).textContent).toContain("选项 A");

    await pressKey(trigger, "ArrowUp");
    expect(highlightedOption(trigger).textContent).toContain("选项 C");

    await pressKey(trigger, "ArrowDown");
    expect(highlightedOption(trigger).textContent).toContain("选项 A");
    await pressKey(trigger, "ArrowDown");
    expect(highlightedOption(trigger).textContent).toContain("选项 B");

    await pressKey(trigger, "Enter");
    expect(onChange).toHaveBeenCalledWith("b");
    expect(container.querySelector('[role="listbox"]')).toBeNull();
    expect(document.activeElement).toBe(trigger);

    await pressKey(trigger, "Enter");
    expect(container.querySelector('[role="listbox"]')).not.toBeNull();
    await pressKey(trigger, "Escape");
    expect(container.querySelector('[role="listbox"]')).toBeNull();
    expect(document.activeElement).toBe(trigger);

    await unmount();
  });

  it("skips disabled options and never opens while disabled", async () => {
    const onChange = vi.fn();
    const options: SelectOption[] = [
      { value: "a", label: "选项 A" },
      { value: "b", label: "选项 B", disabled: true },
      { value: "c", label: "选项 C" },
    ];
    const { container, unmount } = await renderUi(
      <SelectHarness initial="a" options={options} onChange={onChange} />,
    );
    const trigger = byLabel(container, "button", "测试选择") as HTMLButtonElement;

    await pressKey(trigger, "ArrowDown");
    expect(optionNamed(container, "选项 B").getAttribute("aria-disabled")).toBe("true");
    await pressKey(trigger, "ArrowDown");
    expect(highlightedOption(trigger).textContent).toContain("选项 C");
    await pressKey(trigger, "Enter");
    expect(onChange).toHaveBeenCalledWith("c");

    await unmount();

    const disabled = await renderUi(<SelectHarness initial="a" disabled />);
    const disabledTrigger = byLabel(
      disabled.container,
      "button",
      "测试选择",
    ) as HTMLButtonElement;
    expect(disabledTrigger.disabled).toBe(true);
    await clickElement(disabledTrigger);
    expect(disabled.container.querySelector('[role="listbox"]')).toBeNull();
    await pressKey(disabledTrigger, "ArrowDown");
    expect(disabled.container.querySelector('[role="listbox"]')).toBeNull();
    await disabled.unmount();
  });

  it("closes on an outside pointer press", async () => {
    const { container, unmount } = await renderUi(<SelectHarness />);
    const trigger = byLabel(container, "button", "测试选择") as HTMLButtonElement;

    await clickElement(trigger);
    expect(container.querySelector('[role="listbox"]')).not.toBeNull();

    await clickOutside();
    expect(container.querySelector('[role="listbox"]')).toBeNull();
    expect(trigger.getAttribute("aria-expanded")).toBe("false");

    await unmount();
  });
});

describe("DatePicker", () => {
  it("opens a 42-day Monday-first calendar and navigates months", async () => {
    const { container, unmount } = await renderUi(<DateHarness />);
    const trigger = byLabel(container, "button", "测试日期") as HTMLButtonElement;
    const selectedDate = new Date(2026, 10, 6);

    expect(trigger.textContent?.trim()).toBe(formatDate(selectedDate));
    // The staff UI is Chinese-only regardless of the runtime locale.
    expect(trigger.textContent?.trim()).toBe("2026年11月6日");
    await clickElement(trigger);

    const dialog = need(container.querySelector('[role="dialog"]'), "calendar dialog") as HTMLElement;
    expect(trigger.getAttribute("aria-expanded")).toBe("true");
    expect(trigger.getAttribute("aria-controls")).toBe(dialog.id);
    expect(container.querySelectorAll(".fc-day")).toHaveLength(42);
    expect(container.textContent).toContain(monthLabel(new Date(2026, 10, 1)));
    expect(container.textContent).toContain("2026年11月");
    expect(need(
      container.querySelector(".fc-calendar-weekdays span"),
      "weekday heading",
    ).textContent).toBe("周一");
    expect(document.activeElement).toBe(dayButton(container, selectedDate));

    await clickElement(byLabel(container, "button", "下个月"));
    expect(container.textContent).toContain(monthLabel(new Date(2026, 11, 1)));
    await clickElement(byLabel(container, "button", "上个月"));
    expect(container.textContent).toContain(monthLabel(new Date(2026, 10, 1)));
    await clickElement(byLabel(container, "button", "上个月"));
    expect(container.textContent).toContain(monthLabel(new Date(2026, 9, 1)));

    expect(container.querySelector('input[type="date"]')).toBeNull();
    expect(container.querySelector('input[type="datetime-local"]')).toBeNull();
    expect(container.querySelector('input[type="time"]')).toBeNull();

    await unmount();
  });

  it("moves the roving focus with arrows/Home/End/PageUp and selects with Enter", async () => {
    const onChange = vi.fn();
    const { container, unmount } = await renderUi(<DateHarness onChange={onChange} />);
    const trigger = byLabel(container, "button", "测试日期") as HTMLButtonElement;

    await clickElement(trigger);
    await pressKey(activeElement(), "ArrowRight");
    expect(document.activeElement).toBe(dayButton(container, new Date(2026, 10, 7)));

    await pressKey(activeElement(), "ArrowDown");
    expect(document.activeElement).toBe(dayButton(container, new Date(2026, 10, 14)));

    const monday = mondayOf(new Date(2026, 10, 14));
    await pressKey(activeElement(), "Home");
    expect(document.activeElement).toBe(dayButton(container, monday));

    await pressKey(activeElement(), "End");
    const sunday = new Date(monday.getFullYear(), monday.getMonth(), monday.getDate() + 6);
    expect(document.activeElement).toBe(dayButton(container, sunday));

    await pressKey(activeElement(), "PageDown");
    expect(container.textContent).toContain(monthLabel(new Date(2026, 11, 1)));
    const decemberSunday = new Date(2026, 11, sunday.getDate());
    expect(document.activeElement).toBe(dayButton(container, decemberSunday));

    await pressKey(activeElement(), "PageUp");
    expect(container.textContent).toContain(monthLabel(new Date(2026, 10, 1)));
    expect(document.activeElement).toBe(dayButton(container, sunday));

    await pressKey(activeElement(), "Enter");
    expect(onChange).toHaveBeenCalledWith(dateKey(sunday));
    expect(container.querySelector('[role="dialog"]')).toBeNull();
    expect(document.activeElement).toBe(trigger);
    expect(trigger.textContent?.trim()).toBe(formatDate(sunday));
    expect(inputValue(container, 'input[name="picked_date"]')).toBe(dateKey(sunday));

    await unmount();
  });

  it("selects a day with the mouse, clears the value, and closes on Escape with focus restored", async () => {
    const onChange = vi.fn();
    const { container, unmount } = await renderUi(<DateHarness onChange={onChange} />);
    const trigger = byLabel(container, "button", "测试日期") as HTMLButtonElement;

    await clickElement(trigger);
    await clickElement(dayButton(container, new Date(2026, 10, 20)));
    expect(onChange).toHaveBeenCalledWith("2026-11-20");
    expect(container.querySelector('[role="dialog"]')).toBeNull();
    expect(trigger.textContent?.trim()).toBe(formatDate(new Date(2026, 10, 20)));

    await clickElement(trigger);
    await clickElement(buttonWithText(container, "清除"));
    expect(onChange).toHaveBeenLastCalledWith("");
    expect(container.querySelector('[role="dialog"]')).toBeNull();
    expect(trigger.textContent).toContain("选择日期");

    await clickElement(trigger);
    await pressKey(activeElement(), "Escape");
    expect(container.querySelector('[role="dialog"]')).toBeNull();
    expect(document.activeElement).toBe(trigger);

    await unmount();
  });

  it("offers today without a native picker", async () => {
    const onChange = vi.fn();
    const { container, unmount } = await renderUi(
      <DateHarness initial="" onChange={onChange} />,
    );
    const trigger = byLabel(container, "button", "测试日期") as HTMLButtonElement;

    await clickElement(trigger);
    await clickElement(buttonWithText(container, "今天"));
    expect(onChange).toHaveBeenCalledWith(todayKey());
    expect(container.querySelector('[role="dialog"]')).toBeNull();
    expect(trigger.textContent?.trim()).toBe(formatDate(new Date()));

    await unmount();
  });

  it("keeps datetime-local open after choosing a day and commits valid HH:mm only", async () => {
    const onChange = vi.fn();
    const { container, unmount } = await renderUi(
      <DateHarness initial="2026-11-06T09:30" mode="datetime-local" onChange={onChange} />,
    );
    const trigger = byLabel(container, "button", "测试日期") as HTMLButtonElement;

    expect(trigger.textContent).toContain(formatDate(new Date(2026, 10, 6)));
    expect(trigger.textContent).toContain("09:30");
    await clickElement(trigger);

    const initialTime = need(
      container.querySelector('input[aria-label="测试日期 时间"]'),
      "time input",
    ) as HTMLInputElement;
    expect(initialTime.value).toBe("09:30");
    expect(initialTime.getAttribute("inputmode")).toBe("numeric");
    expect(initialTime.disabled).toBe(false);

    await clickElement(dayButton(container, new Date(2026, 10, 10)));
    expect(onChange).toHaveBeenLastCalledWith("2026-11-10T09:30");
    expect(container.querySelector('[role="dialog"]')).not.toBeNull();
    expect(trigger.textContent).toContain("09:30");

    const timeInput = need(
      container.querySelector('input[aria-label="测试日期 时间"]'),
      "time input",
    ) as HTMLInputElement;
    await setControlValue(timeInput, "14:45");
    expect(onChange).toHaveBeenLastCalledWith("2026-11-10T14:45");
    expect(container.querySelector('[role="dialog"]')).not.toBeNull();

    const callsBeforeInvalid = onChange.mock.calls.length;
    await setControlValue(timeInput, "25:99");
    expect(onChange.mock.calls.length).toBe(callsBeforeInvalid);
    expect(timeInput.getAttribute("aria-invalid")).toBe("true");
    expect(
      (need(container.querySelector("output"), "value output") as HTMLOutputElement).textContent,
    ).toBe("2026-11-10T14:45");

    await clickElement(buttonWithText(container, "清除"));
    expect(onChange).toHaveBeenLastCalledWith("");
    expect(container.querySelector('[role="dialog"]')).toBeNull();
    expect(trigger.textContent).toContain("选择日期");

    expect(container.querySelector('input[type="date"]')).toBeNull();
    expect(container.querySelector('input[type="datetime-local"]')).toBeNull();
    expect(container.querySelector('input[type="time"]')).toBeNull();
    expect(container.querySelector("select")).toBeNull();

    await unmount();
  });

  it("defaults to 00:00 when a day is chosen before any time is entered", async () => {
    const onChange = vi.fn();
    const { container, unmount } = await renderUi(
      <DateHarness initial="" mode="datetime-local" onChange={onChange} />,
    );
    const trigger = byLabel(container, "button", "测试日期") as HTMLButtonElement;

    await clickElement(trigger);
    const disabledTime = need(
      container.querySelector('input[aria-label="测试日期 时间"]'),
      "time input",
    ) as HTMLInputElement;
    expect(disabledTime.disabled).toBe(true);

    await clickElement(dayButton(container, new Date(2026, 10, 12)));
    expect(onChange).toHaveBeenLastCalledWith("2026-11-12T00:00");
    expect(container.querySelector('[role="dialog"]')).not.toBeNull();

    const enabledTime = need(
      container.querySelector('input[aria-label="测试日期 时间"]'),
      "time input",
    ) as HTMLInputElement;
    expect(enabledTime.disabled).toBe(false);
    expect(enabledTime.value).toBe("00:00");

    await unmount();
  });
});
