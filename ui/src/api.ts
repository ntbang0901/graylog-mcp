import { token } from "./store";
import { ROOT } from "./router";
export { token } from "./store";

// The token comes once in the link the CLI opens (#token=), then stays in this browser for this address, so the
// page's other addresses (a link to a view, a reload) carry none.
export function initToken() {
    const params = new URLSearchParams(location.hash.slice(1));
    token.value = params.get("token") || "";
    try {
        token.value ||=
            localStorage.getItem("gm-token") ||
            sessionStorage.getItem("gm-token") || // earlier versions
            "";
        if (token.value) localStorage.setItem("gm-token", token.value);
    } catch {
        /* storage blocked: the token lasts as long as the page */
    }
    // the address keeps its path and loses the token
    if (params.has("token"))
        history.replaceState(null, "", location.pathname + location.search);
}
// a wrong or old token: the address stays as it is, so reloading after signing in opens the same view
function signedOut() {
    token.value = "";
    try {
        localStorage.removeItem("gm-token");
    } catch {
        /* storage blocked */
    }
}
export function base() {
    // without a page (the store's tests in Node): the folder of the address
    return typeof document !== "undefined"
        ? ROOT()
        : location.pathname.replace(/\/[^/]*$/, "");
}
export async function api<T = Record<string, unknown>>(
    path: string,
    method = "GET",
    body?: unknown,
): Promise<T> {
    const res = await fetch(base() + path, {
        method,
        headers: {
            "X-Admin-Token": token.value,
            "Content-Type": "application/json",
        },
        body: body === undefined ? undefined : JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({ error: res.statusText }));
    if (res.status === 401) signedOut();
    if (!res.ok) throw new Error(data.error || res.statusText);
    return data as T;
}
export const errorText = (e: unknown) =>
    e instanceof Error ? e.message : String(e);
export const storage = {
    get(key: string) {
        try {
            return localStorage.getItem(key);
        } catch {
            return null;
        }
    },
    set(key: string, value: string | null) {
        try {
            if (value === null) localStorage.removeItem(key);
            else localStorage.setItem(key, value);
        } catch {
            /* storage blocked */
        }
    },
};
