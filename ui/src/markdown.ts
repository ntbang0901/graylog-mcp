// Ticket-ready Markdown of what a screen shows. The text is already redacted by the server; here it is only made
// safe to paste: a table cell keeps to one line and a long value is cut. No DOM and no store here.

export const MAX_CELL = 300;
export const MAX_MESSAGE = 4000;

export function cap(text: string, max: number): string {
    return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}
function text(v: unknown): string {
    if (v == null) return "";
    return typeof v === "object" ? JSON.stringify(v) : String(v);
}
// a pipe would end the cell and a newline the row
export function cell(v: unknown, max = MAX_CELL): string {
    return cap(text(v), max)
        .replace(/\\/g, "\\\\")
        .replace(/\|/g, "\\|")
        .replace(/\r?\n/g, "<br>");
}
// text in a heading or a list item: Markdown's own characters are taken literally
export function inline(v: unknown, max = MAX_CELL): string {
    return cap(text(v), max)
        .replace(/\r?\n/g, " ")
        .replace(/([\\`*_[\]<>#|])/g, "\\$1");
}
export function table(head: string[], rows: unknown[][]): string {
    return [
        `| ${head.map((h) => cell(h)).join(" | ")} |`,
        `| ${head.map(() => "---").join(" | ")} |`,
        ...rows.map((r) => `| ${r.map((v) => cell(v)).join(" | ")} |`),
    ].join("\n");
}
// a fence longer than any run of backticks in the text, so the text cannot close it
export function fenced(body: string, lang = "text"): string {
    const longest = Math.max(
            0,
            ...(body.match(/`+/g) || []).map((s) => s.length),
        ),
        fence = "`".repeat(Math.max(3, longest + 1));
    return `${fence}${lang}\n${body}\n${fence}`;
}

export interface TraceLine {
    time: string;
    offset_ms?: number | null;
    service: string;
    level: string;
    message: unknown;
}
export interface TraceInfo {
    trace: string;
    instance: string;
    range?: { from: string; to: string } | null;
    total?: number | null;
    duration_ms?: number | null;
    lines: TraceLine[];
}
export function traceMarkdown(t: TraceInfo): string {
    const facts = [`**Environment:** ${inline(t.instance)}`];
    if (t.range)
        facts.push(
            `**Range:** ${inline(t.range.from)} to ${inline(t.range.to)}`,
        );
    const shown =
        t.total != null && t.total > t.lines.length
            ? `${t.lines.length} of ${t.total}`
            : String(t.lines.length);
    facts.push(`**Messages:** ${shown}`);
    if (t.duration_ms != null) facts.push(`**Duration:** ${t.duration_ms} ms`);
    const rows = t.lines.map((l) => [
        l.time,
        l.offset_ms == null ? "" : `+${l.offset_ms} ms`,
        l.service,
        l.level,
        l.message,
    ]);
    return [
        `## Trace ${inline(t.trace)}`,
        "",
        facts.map((f) => `- ${f}`).join("\n"),
        "",
        table(["Time", "Offset", "Service", "Level", "Message"], rows),
        "",
    ].join("\n");
}

export interface MessageInfo {
    instance: string;
    ts?: unknown;
    source?: unknown;
    service?: { field: string; value: unknown } | null;
    level: string;
    message: unknown;
    fields: Record<string, unknown>;
}
export function messageMarkdown(m: MessageInfo): string {
    const facts = [
        ["Time", m.ts],
        ["Environment", m.instance],
        ["Source", m.source],
        ...(m.service
            ? [[`Service (${m.service.field})`, m.service.value]]
            : []),
        ["Level", m.level],
    ].filter(([, v]) => v != null && v !== "");
    const fields = Object.entries(m.fields);
    return [
        "## Log message",
        "",
        facts.map(([k, v]) => `- **${inline(k)}:** ${inline(v)}`).join("\n"),
        "",
        fenced(cap(text(m.message), MAX_MESSAGE)),
        ...(fields.length ? ["", table(["Field", "Value"], fields)] : []),
        "",
    ].join("\n");
}
