import { useState } from "preact/hooks";
import { openTrace } from "../explorer-store";
import { firstLine, fmtMs, type TraceAnswer } from "../explorer";
import type { Journey } from "../scripts/types";
import { TraceTimeline } from "./TraceTimeline";
import { num, plural } from "./UI";

const HOW: Record<string, string> = {
    trace_fields: "Followed by its trace id",
    code_field: "Found in the code's own field",
    full_text: "Found as text in the messages",
    business_code: "A business code: the request it names is followed",
};
const NO_FIELDS = {
    service: null,
    endpoint: null,
    status: null,
    latency: null,
    trace: null,
};
// the timeline of trace_request as the Logs screen's trace answer, so it is drawn and copied the same way
function asTrace(j: Journey, trace: string): TraceAnswer {
    const lines = j.timeline || [];
    return {
        instance: j.instance,
        range: j.range,
        query: "",
        fields: NO_FIELDS,
        trace,
        total: j.total ?? lines.length,
        shown: lines.length,
        duration_ms: j.duration_ms ?? null,
        messages: lines.map((l) => ({
            ...l,
            offset_ms: Number(String(l.t).replace(/[^\d.]/g, "")) || 0,
            service: l.service || null,
        })),
    };
}

// the trace_request tool's answer for a pasted code: how it was found, the requests it went through, its lines
export function JourneyView({ j }: { j: Journey }) {
    const [open, setOpen] = useState<ReadonlySet<string>>(new Set()),
        followed =
            (j.strategy === "business_code" ? j.shown_trace : j.trace_id) ||
            j.trace_id,
        lines = j.timeline || [],
        services = (j.services || []).map((s) => s.service),
        first = j.first_error;
    const toggle = (key: string) => {
        const next = new Set(open);
        if (!next.delete(key)) next.add(key);
        setOpen(next);
    };
    return (
        <div class="ask-journey">
            <div class="ask-jhead">
                <h3 class="mono truncate">{j.trace_id}</h3>
                <span class="muted small">
                    {j.strategy ? HOW[j.strategy] || j.strategy : ""} ·{" "}
                    {j.instance}
                </span>
                <span class="spacer" />
                {lines.length > 0 && followed && (
                    <button
                        class="btn sm"
                        type="button"
                        title="Read this request on the By trace tab"
                        onClick={() =>
                            void openTrace(j.instance, followed, j.range)
                        }
                    >
                        Open in By trace
                    </button>
                )}
            </div>
            {lines.length > 0 && (
                <div class="small">
                    {plural(j.total ?? lines.length, "message")}
                    {services.length
                        ? ` across ${plural(services.length, "service")}: ${services.join(" → ")}`
                        : ""}
                    {j.duration_ms != null ? ` · ${fmtMs(j.duration_ms)}` : ""}
                    {first && (
                        <>
                            {" "}
                            · first error in <b>{first.service}</b>:{" "}
                            <span class="mono">
                                {firstLine(first.message).slice(0, 160)}
                            </span>
                        </>
                    )}
                </div>
            )}
            {j.hint && <div class="inline-msg mt-xs">{j.hint}</div>}
            {j.note && <div class="hint mt-xs">{j.note}</div>}
            {!!j.traces?.length && (
                <div class="table-wrap mt-xs">
                    <table class="lg-table">
                        <thead>
                            <tr>
                                <th>Request</th>
                                <th>First</th>
                                <th>Last</th>
                                <th class="num">Messages</th>
                                <th class="num">Errors</th>
                                <th>Services</th>
                            </tr>
                        </thead>
                        <tbody>
                            {j.traces.map((t) => (
                                <tr key={t.trace_id}>
                                    <td class="mono">
                                        <button
                                            type="button"
                                            class="lg-link"
                                            title="Open on the By trace tab"
                                            onClick={() =>
                                                void openTrace(
                                                    j.instance,
                                                    t.trace_id,
                                                    j.range,
                                                )
                                            }
                                        >
                                            {t.trace_id}
                                        </button>
                                    </td>
                                    <td class="mono">{t.first || "-"}</td>
                                    <td class="mono">{t.last || "-"}</td>
                                    <td class="num mono">
                                        {t.messages == null
                                            ? "-"
                                            : num(t.messages)}
                                    </td>
                                    <td class="num mono">
                                        {t.errors == null ? "-" : num(t.errors)}
                                    </td>
                                    <td>{(t.services || []).join(", ")}</td>
                                </tr>
                            ))}
                        </tbody>
                    </table>
                </div>
            )}
            {lines.length > 0 && (
                <div class="mt-xs">
                    <TraceTimeline
                        trace={asTrace(j, followed)}
                        open={open}
                        onToggle={toggle}
                    />
                </div>
            )}
        </div>
    );
}
