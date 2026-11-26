import dayjs from "dayjs";
import utc from "dayjs/plugin/utc.js";
import timezone from "dayjs/plugin/timezone.js";

dayjs.extend(utc);
dayjs.extend(timezone);

export type TimeValue = string | number | Date | null | undefined;

// API numbers are epoch milliseconds. Callers with epoch seconds multiply by 1000.
// Graylog strings without an offset represent UTC, just as they do on the server.
export function parseTime(value: TimeValue) {
    if (value == null || (typeof value === "string" && !value.trim()))
        return null;
    const at = dayjs.utc(
        typeof value === "string" ? value.trim().replace(" ", "T") : value,
    );
    return at.isValid() ? at.local() : null;
}

export function formatTime(
    value: TimeValue,
    pattern = "HH:mm:ss",
    fallback = "-",
) {
    return parseTime(value)?.format(pattern) ?? fallback;
}

export const formatDateTime = (value: TimeValue, fallback = "-") =>
    formatTime(value, "YYYY-MM-DD HH:mm:ss [UTC]Z", fallback);

// Older saved charts may only have wall-clock labels; those cannot be converted as instants.
export const absoluteTime = (value: string) =>
    /^\d{4}-\d{2}-\d{2}[T ].*(?:Z|[+-]\d{2}:?\d{2})$/i.test(value)
        ? parseTime(value)
        : null;

export function viewZone() {
    try {
        return dayjs.tz.guess() || "UTC";
    } catch {
        return "UTC";
    }
}

// datetime-local is a wall clock in the browser's zone, rather than an API timestamp.
export const localInput = (value: TimeValue) =>
    formatTime(value, "YYYY-MM-DDTHH:mm", "");

export function localInputToUtc(value: string) {
    if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(value)) return null;
    const at = dayjs(value);
    // Reject invalid calendar dates and wall clocks skipped by a DST transition.
    return at.isValid() && at.format("YYYY-MM-DDTHH:mm") === value
        ? at.toISOString()
        : null;
}

export function whenShort(ts: number, now = Date.now()) {
    const at = parseTime(ts * 1000);
    if (!at) return "-";
    return at.format(at.isSame(dayjs(now), "day") ? "HH:mm:ss" : "ddd HH:mm");
}

export interface Zoned {
    ymd: string;
    day: string;
    time: string;
    seconds: string;
    zone: string;
}

export function zoned(value: TimeValue, tz: string = viewZone()): Zoned | null {
    let at = parseTime(value);
    if (!at) return null;
    try {
        at = at.tz(tz || "UTC");
    } catch {
        at = at.tz("UTC");
    }
    return {
        ymd: at.format("YYYY-MM-DD"),
        day: at.format("D MMM"),
        time: at.format("HH:mm"),
        seconds: at.format("HH:mm:ss"),
        zone: at.offsetName("short") || "UTC",
    };
}
