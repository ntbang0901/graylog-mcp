// Each view of the page has its own address, ROOT/<page>[/<tab>[/<id>]]?<filters>, so a link or a reload opens it
// again. Changing page, tab or filter adds to the history (typing a filter replaces the entry); Back and Forward
// render what the address says.

// where the page is served: "" for 'graylog-mcp ui', "/admin" under the shared server; the server writes it into
// the page (app.py), and a <base> would be refused by the CSP. Read on first use: the store loads without a DOM.
let root: string | null = null;
export function ROOT(): string {
    if (root === null) {
        const meta =
            typeof document === "undefined"
                ? null
                : document.querySelector<HTMLMetaElement>(
                      'meta[name="graylog-mcp-root"]',
                  );
        root = (meta?.content ?? "").replace(/^__.*__$/, "").replace(/\/$/, "");
    }
    return root;
}

export interface Route {
    page: string;
    sub: string[];
    query: URLSearchParams;
}
export type How = "push" | "replace" | "none";

export function readRoute(loc: Location = location): Route {
    const rest = loc.pathname.startsWith(`${ROOT()}/`)
        ? loc.pathname.slice(ROOT().length + 1)
        : "";
    const [page = "", ...sub] = rest
        .split("/")
        .filter(Boolean)
        .map((s) => {
            try {
                return decodeURIComponent(s);
            } catch {
                return s;
            }
        });
    return {
        page: page || "home",
        sub,
        query: new URLSearchParams(loc.search),
    };
}

export function pathOf(
    page: string,
    sub: string[] = [],
    query: URLSearchParams | null = null,
): string {
    const q = query ? query.toString() : "";
    const parts =
        page === "home" ? "" : [page, ...sub].map(encodeURIComponent).join("/");
    return `${ROOT()}/${parts}${q ? `?${q}` : ""}`;
}

// "push" adds a history entry, "replace" changes the current one, "none" leaves the address (it was the address
// that changed)
export function setAddress(url: string, how: How = "push") {
    if (how === "none" || url === location.pathname + location.search) return;
    history[how === "replace" ? "replaceState" : "pushState"](null, "", url);
}

// A screen with views of its own (tabs, filters, an open item) says how to write them into its address and how to
// read them back. Screens without one are just ROOT/<page>.
interface Screen {
    path: () => string;
    apply: (r: Route) => void;
}
const screens: Record<string, Screen> = {};
export function registerScreen(page: string, screen: Screen) {
    screens[page] = screen;
}
export function pagePath(page: string): string {
    return screens[page]?.path() ?? pathOf(page);
}
export function applyRoute(r: Route) {
    screens[r.page]?.apply(r);
}
