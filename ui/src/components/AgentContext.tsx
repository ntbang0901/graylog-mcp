import type { ContextItem } from "../contexts-model";
import {
    contextDraft,
    contextError,
    contextLoading,
    contextOpen,
    contextSaving,
    copyContextPrompt,
    editContext,
    openContext,
    openSavedContext,
    recentContexts,
    saveContext,
    savedContext,
    showContextDraft,
} from "../contexts";
import { logsView } from "../logs";
import { formatDateTime } from "../time";
import { AsyncButton, Dialog, Icon } from "./UI";

export function AgentContextButton() {
    const n = contextDraft.value.items.length;
    return (
        <button
            class="ctl agent-context-button"
            type="button"
            onClick={openContext}
            aria-haspopup="dialog"
            aria-controls="agentContext"
            aria-label={`Agent context, ${n} draft items`}
        >
            <Icon name="lines" />
            <span>Agent context</span>
            {!!n && (
                <span class="badge" aria-live="polite">
                    {n}
                </span>
            )}
        </button>
    );
}

function Evidence({
    item,
    index,
    editable,
}: {
    item: ContextItem;
    index: number;
    editable: boolean;
}) {
    const scope = item.scope;
    return (
        <li class="ctx-item">
            <div class="ctx-item-head">
                <span class="badge">{item.kind}</span>
                <span class="ctx-item-title">{item.title}</span>
                {editable && (
                    <button
                        class="btn sm ghost"
                        type="button"
                        disabled={contextSaving.value}
                        aria-label={`Remove ${item.title}`}
                        onClick={() =>
                            editContext({
                                items: contextDraft.value.items.filter(
                                    (_, i) => i !== index,
                                ),
                            })
                        }
                    >
                        Remove
                    </button>
                )}
            </div>
            <p class="muted small">
                {scope.instance} · {formatDateTime(scope.from_time)} –{" "}
                {formatDateTime(scope.to_time)}
            </p>
            <details>
                <summary>Query and captured evidence</summary>
                <code class="ctx-query">{scope.query}</code>
                <p class="muted small">
                    Streams: {scope.streams.join(", ") || "all"} · source zone:{" "}
                    {scope.timezone} · captured{" "}
                    {formatDateTime(item.captured_at)}
                </p>
                {item.limits.map((limit) => (
                    <p class="muted small">{limit}</p>
                ))}
                <pre class="ctx-data">{JSON.stringify(item.data, null, 2)}</pre>
            </details>
        </li>
    );
}

export function AgentContext() {
    const draft = contextDraft.value,
        saved = savedContext.value,
        items = saved?.items || draft.items,
        busy = contextSaving.value || contextLoading.value,
        close = () => {
            contextOpen.value = false;
        };
    return (
        <Dialog
            id="agentContext"
            class="sheet ctx-sheet"
            aria-labelledby="ctxTitle"
            open={contextOpen.value}
            onClose={close}
        >
            <div class="dhead">
                <div class="min0">
                    <h2 id="ctxTitle">Agent context</h2>
                    <p class="muted small">
                        {saved
                            ? `${saved.id} · saved snapshot`
                            : `Draft · ${draft.items.length} of 20 items`}
                    </p>
                </div>
                <span class="spacer" />
                <button class="btn sm ghost" type="button" onClick={close}>
                    {logsView.value ? "Back to logs" : "Close"}
                </button>
            </div>
            <div class="ctx-body">
                {contextError.value && (
                    <div class="banner warn" role="alert">
                        {contextError.value}
                    </div>
                )}
                {contextLoading.value ? (
                    <p role="status">Loading saved context…</p>
                ) : (
                    <>
                        {saved ? (
                            <>
                                <div class="ctx-saved-head">
                                    <h3>{saved.title}</h3>
                                    <button
                                        class="btn sm"
                                        type="button"
                                        disabled={busy}
                                        onClick={showContextDraft}
                                    >
                                        {draft.items.length
                                            ? `Back to draft (${draft.items.length})`
                                            : "New context"}
                                    </button>
                                </div>
                                <p class="ctx-question">{saved.question}</p>
                                <p class="muted small">
                                    Saved {formatDateTime(saved.created)}. This
                                    snapshot stays unchanged.
                                </p>
                            </>
                        ) : (
                            <>
                                <label class="ctx-label" for="ctxName">
                                    Title
                                    <input
                                        id="ctxName"
                                        value={draft.title}
                                        maxLength={160}
                                        disabled={busy}
                                        placeholder="Checkout failures"
                                        onInput={(e) =>
                                            editContext({
                                                title: e.currentTarget.value,
                                            })
                                        }
                                    />
                                </label>
                                <label class="ctx-label" for="ctxQuestion">
                                    What should the agent investigate?
                                    <textarea
                                        id="ctxQuestion"
                                        value={draft.question}
                                        maxLength={2000}
                                        rows={3}
                                        disabled={busy}
                                        placeholder="Describe what you observed, what you expected, and what you need the agent to do."
                                        onInput={(e) =>
                                            editContext({
                                                question: e.currentTarget.value,
                                            })
                                        }
                                    />
                                </label>
                            </>
                        )}
                        <section aria-label="Collected evidence">
                            <h3>
                                Collected evidence{" "}
                                <span class="muted">({items.length})</span>
                            </h3>
                            {items.length ? (
                                <ol class="ctx-items">
                                    {items.map((item, index) => (
                                        <Evidence
                                            key={
                                                item.id ||
                                                `${index}:${item.captured_at}`
                                            }
                                            item={item}
                                            index={index}
                                            editable={!saved}
                                        />
                                    ))}
                                </ol>
                            ) : (
                                <div class="ctx-empty">
                                    <Icon name="lines" />
                                    <p>
                                        Add a search, log, or trace from Logs or
                                        the log drawer. You can keep browsing as
                                        you collect evidence.
                                    </p>
                                </div>
                            )}
                        </section>
                        {saved && (
                            <label class="ctx-label" for="ctxPrompt">
                                Prompt to paste into your agent chat
                                <textarea
                                    id="ctxPrompt"
                                    class="mono"
                                    rows={5}
                                    readOnly
                                    value={saved.prompt}
                                    onFocus={(e) => e.currentTarget.select()}
                                />
                            </label>
                        )}
                    </>
                )}
                <details class="ctx-history">
                    <summary>
                        Saved contexts ({recentContexts.value.length})
                    </summary>
                    <p class="muted small">
                        The most recent 100 snapshots are kept on this machine.
                    </p>
                    {recentContexts.value.length ? (
                        <ul>
                            {recentContexts.value.map((row) => (
                                <li key={row.id}>
                                    <button
                                        type="button"
                                        class={`ctx-history-row${saved?.id === row.id ? " on" : ""}`}
                                        disabled={busy}
                                        onClick={() =>
                                            void openSavedContext(row.id)
                                        }
                                    >
                                        <span>{row.title}</span>
                                        <small class="muted">
                                            {row.id} · {row.item_count} items ·{" "}
                                            {formatDateTime(row.created)}
                                        </small>
                                    </button>
                                </li>
                            ))}
                        </ul>
                    ) : (
                        <p class="muted small">No saved context yet.</p>
                    )}
                </details>
            </div>
            <div class="dfoot ctx-foot">
                <p class="muted small">
                    {saved
                        ? "Paste the prompt into an agent connected to this Graylog MCP server."
                        : "Each item keeps its captured time window. Saving applies your redaction rules."}
                </p>
                {saved ? (
                    <AsyncButton
                        class="btn primary"
                        disabled={busy}
                        run={() => copyContextPrompt(saved)}
                    >
                        Copy agent prompt
                    </AsyncButton>
                ) : (
                    <AsyncButton
                        class="btn primary"
                        disabled={
                            busy ||
                            !draft.items.length ||
                            !draft.title.trim() ||
                            !draft.question.trim()
                        }
                        run={saveContext}
                    >
                        Save &amp; copy prompt
                    </AsyncButton>
                )}
            </div>
        </Dialog>
    );
}
