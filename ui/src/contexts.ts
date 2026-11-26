import { signal } from "@preact/signals";
import { api, errorText } from "./api";
import { toast } from "./store";
import {
    selectionKey,
    type ContextDraft,
    type ContextItem,
    type ContextSummary,
    type SavedContext,
} from "./contexts-model";

export const contextOpen = signal(false);
export const contextDraft = signal<ContextDraft>({
    title: "",
    question: "",
    items: [],
});
export const savedContext = signal<SavedContext | null>(null);
export const recentContexts = signal<ContextSummary[]>([]);
export const contextError = signal("");
export const contextLoading = signal(false);
export const contextSaving = signal(false);
let generation = 0;

export function addContextItem(item: ContextItem) {
    if (contextSaving.value) {
        toast(
            "Context is saving. Add this evidence again when it finishes.",
            "fail",
        );
        return;
    }
    const draft = contextDraft.value;
    if (draft.items.some((i) => selectionKey(i) === selectionKey(item))) {
        toast("Already in agent context");
        return;
    }
    if (draft.items.length >= 20) {
        toast(
            "A context can hold 20 items. Review it before adding more.",
            "fail",
        );
        return;
    }
    generation++;
    contextLoading.value = false;
    savedContext.value = null;
    contextError.value = "";
    contextDraft.value = {
        ...draft,
        title: draft.title || item.title,
        items: [...draft.items, structuredClone(item)],
    };
    toast(`Added to agent context · ${draft.items.length + 1} items`);
}

export function editContext(patch: Partial<ContextDraft>) {
    if (!contextSaving.value)
        contextDraft.value = { ...contextDraft.value, ...patch };
}

export function showContextDraft() {
    generation++;
    contextLoading.value = false;
    savedContext.value = null;
    contextError.value = "";
}

export async function loadRecentContexts() {
    try {
        const result = await api<{ contexts: ContextSummary[] }>(
            "/api/contexts",
        );
        recentContexts.value = result.contexts;
    } catch (e) {
        contextError.value = errorText(e);
    }
}

export function openContext() {
    contextOpen.value = true;
    void loadRecentContexts();
}

export async function openSavedContext(id: string) {
    const mine = ++generation;
    contextLoading.value = true;
    contextError.value = "";
    try {
        const saved = await api<SavedContext>(
            `/api/contexts/${encodeURIComponent(id)}`,
        );
        if (mine === generation) savedContext.value = saved;
    } catch (e) {
        if (mine === generation) contextError.value = errorText(e);
    } finally {
        if (mine === generation) contextLoading.value = false;
    }
}

export async function copyContextPrompt(saved: SavedContext) {
    try {
        await navigator.clipboard.writeText(saved.prompt);
        contextError.value = "";
        toast(`Copied ${saved.id} · paste it into your agent chat`);
    } catch {
        contextError.value = `${saved.id} is saved. Clipboard is unavailable; select and copy the prompt below.`;
    }
}

export async function saveContext() {
    if (contextSaving.value) return;
    contextSaving.value = true;
    contextError.value = "";
    try {
        const saved = await api<SavedContext>(
            "/api/contexts",
            "POST",
            contextDraft.value,
        );
        savedContext.value = saved;
        contextDraft.value = { title: "", question: "", items: [] };
        await copyContextPrompt(saved);
        await loadRecentContexts();
    } catch (e) {
        contextError.value = errorText(e);
    } finally {
        contextSaving.value = false;
    }
}
