import { state } from "../store";
const BASE_MASKS = [
    "REDACTED",
    "EMAIL",
    "CARD",
    "PHONE",
    "PRIVATE_KEY",
    "JWT",
    "VN_ID",
    "SSN",
    "IBAN",
    "NINO",
    "AADHAAR",
    "PAN",
    "IP",
].map((t) => `[${t}]`);
const reEsc = (t: string) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
export function maskRegex(extra?: string[]) {
    const custom =
        extra ||
        (
            (state.value?.data.redaction?.patterns || []) as {
                replacement?: string;
            }[]
        )
            .map((p) => p.replacement || "")
            .filter(Boolean);
    return new RegExp(
        [...new Set([...BASE_MASKS, ...custom])]
            .sort((a, b) => b.length - a.length)
            .map(reEsc)
            .join("|"),
        "g",
    );
}
export function Marks({ text, extra }: { text: string; extra?: string[] }) {
    const re = maskRegex(extra),
        parts = [];
    let from = 0;
    for (const hit of text.matchAll(re)) {
        parts.push(text.slice(from, hit.index), <mark>{hit[0]}</mark>);
        from = hit.index + hit[0].length;
    }
    parts.push(text.slice(from));
    return <>{parts}</>;
}
