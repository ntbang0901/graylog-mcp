import { levelOf } from "../logs";
import {
    firstLine,
    fmtMs,
    type ExplorerMessage,
    type TraceAnswer,
} from "../explorer";
import type { Json } from "../scripts/types";
import { JsonTree } from "./JsonTree";
import { Marks } from "./Marks";
import { num, plural } from "./UI";

export function LevelBadge({ level }: { level: unknown }) {
    const lv = levelOf(level);
    return <span class={`badge ${lv.cls}`}>{lv.word}</span>;
}
// every field as a tree; a multi-line message (a stack trace) folded above it
export function MessageDetail({ m }: { m: ExplorerMessage }) {
    const lines = String(m.full_message || m.message || "").split("\n"),
        rest: ExplorerMessage = { ...m };
    delete rest.full_message;
    return (
        <div class="lg-wrap">
            {lines.length > 1 && (
                <details class="lg-stack">
                    <summary>
                        {lines[0]}{" "}
                        <span class="muted">
                            · {plural(lines.length - 1, "more line")}
                        </span>
                    </summary>
                    <pre class="json">
                        <Marks text={lines.slice(1).join("\n")} />
                    </pre>
                </details>
            )}
            <div class="tree">
                <JsonTree value={rest as Json} />
            </div>
        </div>
    );
}
export const rowKey = (m: ExplorerMessage, i: number) => m.ref || `i${i}`;

// One trace's messages in time order, a row per message with its place on the trace's span. Takes the answer of
// /api/explorer/trace/<id> and the keys of the rows to show open, so another screen can show a request the same way.
export function TraceTimeline({
    trace,
    open,
    onToggle,
}: {
    trace: TraceAnswer;
    open: ReadonlySet<string>;
    onToggle?: (key: string) => void;
}) {
    const d = trace;
    if (!d.messages?.length)
        return (
            <div class="allclear">// no message of this trace in the range</div>
        );
    const span = Math.max(1, d.duration_ms || 0),
        services = [...new Set(d.messages.map((m) => m.service || "-"))];
    return (
        <>
            <div class="cmeta">
                <span>
                    {plural(d.total, "message")}
                    {d.shown < d.total
                        ? `, the first ${num(d.shown)} shown`
                        : ""}
                    {d.duration_ms == null ? "" : ` · ${fmtMs(d.duration_ms)}`}{" "}
                    · {plural(services.length, "service")}:{" "}
                    {services.join(", ")}
                </span>
            </div>
            <div class="lg-tline">
                {d.messages.map((m, i) => {
                    const key = rowKey(m, i),
                        isOpen = open.has(key),
                        at = m.offset_ms || 0,
                        lv = levelOf(m.level);
                    return (
                        <div
                            key={key}
                            class={`lg-tcall${isOpen ? " open" : ""}`}
                        >
                            <button
                                type="button"
                                class="lg-thead"
                                aria-expanded={isOpen}
                                disabled={!onToggle}
                                onClick={() => onToggle?.(key)}
                            >
                                <span class="mono muted num">+{fmtMs(at)}</span>
                                <span class="lg-svc truncate">
                                    {m.service || "-"}
                                </span>
                                <span class="lg-what truncate">
                                    <Marks text={firstLine(m.message)} />
                                </span>
                                <span class={`badge ${lv.cls}`}>{lv.word}</span>
                                <span class="lg-tbar" aria-hidden="true">
                                    <span
                                        class={lv.cls === "fail" ? "bad" : ""}
                                        style={{
                                            left: `${Math.min(99.4, (100 * at) / span).toFixed(2)}%`,
                                        }}
                                    />
                                </span>
                            </button>
                            {isOpen && (
                                <div class="lg-tdet">
                                    <MessageDetail m={m} />
                                </div>
                            )}
                        </div>
                    );
                })}
            </div>
        </>
    );
}
