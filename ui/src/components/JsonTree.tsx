import type { Json } from "../scripts/types";
import { Marks } from "./Marks";
import { plural } from "./UI";

export function JsonTree({
    value,
    name = null,
    depth = 0,
}: {
    value: Json;
    name?: string | null;
    depth?: number;
}) {
    const key =
        name === null ? null : (
            <>
                <span class="jk">{JSON.stringify(name)}</span>
                <span class="tc">: </span>
            </>
        );
    if (value === null || typeof value !== "object")
        return (
            <div class="tl">
                {key}
                {typeof value === "string" ? (
                    <span class="js">
                        <Marks text={JSON.stringify(value)} />
                    </span>
                ) : typeof value === "number" ? (
                    <span class="jn">{value}</span>
                ) : (
                    <span class="jb">{String(value)}</span>
                )}
            </div>
        );
    const arr = Array.isArray(value),
        entries: [string | null, Json][] = arr
            ? value.map((x) => [null, x])
            : Object.entries(value),
        [o, c] = arr ? ["[", "]"] : ["{", "}"];
    if (!entries.length)
        return (
            <div class="tl">
                {key}
                <span class="tc">
                    {o}
                    {c}
                </span>
            </div>
        );
    const keys = Object.keys(value);
    return (
        <details open={depth < 2}>
            <summary>
                {key}
                <span class="tc">{o}</span>
                <span class="tc gone-open">
                    {" "}
                    {arr
                        ? plural(entries.length, "item")
                        : keys.slice(0, 4).join(", ") +
                          (keys.length > 4 ? `, +${keys.length - 4}` : "")}{" "}
                    {c}
                </span>
            </summary>
            {entries.map(([k, v], i) => (
                <JsonTree key={k ?? i} value={v} name={k} depth={depth + 1} />
            ))}
            <div class="tl tc">{c}</div>
        </details>
    );
}
