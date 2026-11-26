// The investigation's event stream. EventSource cannot send the admin token header, so this reads the response
// with fetch; frames are parsed by investigations-model.ts.
import { parseFrames, type Frame } from "./investigations-model";

export async function readEvents(
    url: string,
    token: string,
    onFrame: (f: Frame) => void,
    signal: AbortSignal,
    onOpen: () => void = () => {},
): Promise<void> {
    const res = await fetch(url, {
        headers: { "X-Admin-Token": token, Accept: "text/event-stream" },
        signal,
    });
    if (!res.ok || !res.body)
        throw new Error(res.statusText || `HTTP ${res.status}`);
    onOpen(); // the stream is up even while nothing happens: keep-alives are comments, not frames
    const reader = res.body.getReader(),
        decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
        const { done, value } = await reader.read();
        if (done) return;
        buffer += decoder.decode(value, { stream: true });
        const { frames, rest } = parseFrames(buffer);
        buffer = rest;
        for (const f of frames) onFrame(f);
    }
}
