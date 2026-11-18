import { api, base } from "./api";
import {
    instances,
    loadSetup,
    loadState,
    openEnv,
    setup,
    toast,
} from "./store";
import { plural } from "./components/UI";
export function setupSteps() {
    const s = setup.value;
    if (!s) return [];
    const { server: srv, claude: cl } = s;
    const steps = [
        srv.running
            ? {
                  key: "server",
                  ok: true,
                  what: "Server running",
                  why: `One process for every Claude session, v${srv.version || "?"}${srv.commit ? ` (${srv.commit.slice(0, 7)})` : ""}`,
                  auto: false,
              }
            : {
                  key: "server",
                  ok: false,
                  auto: true,
                  what: "Start the server",
                  why: "One process shared by every Claude session; installs a stable copy the first time (about a minute).",
              },
    ];
    if (s.autostart.kind)
        steps.push({
            key: "autostart",
            ok: s.autostart.enabled,
            auto: !s.autostart.enabled,
            what: s.autostart.enabled
                ? "Starts when you log in"
                : "Start it when you log in",
            why: s.autostart.enabled
                ? `Managed by ${s.autostart.kind}, restarted if it crashes`
                : "So you never have to start it again after a restart.",
        });
    const moving = [
        ...cl.stdio_projects,
        ...cl.stale_projects,
        ...cl.stale_files,
        ...cl.missing_projects,
    ];
    if (!moving.length && (cl.registered || cl.repos))
        steps.push({
            key: "claude",
            ok: true,
            auto: false,
            what: "Claude Code connected",
            why: cl.registered
                ? "In every project. Open a new Claude session to use it."
                : "In the repositories of your groups. Open a new Claude session to use it.",
        });
    else if (moving.length && cl.cli)
        steps.push({
            key: "claude",
            ok: false,
            auto: true,
            what: `Connect ${plural(moving.length, "repository", "repositories")} to the shared server`,
            why: `They start their own process, still use another port or are not connected yet: ${moving.join(", ")}. Only your own Claude settings change; nothing is written in the repositories.`,
        });
    else if (!moving.length)
        steps.push({
            key: "claude",
            ok: true,
            auto: false,
            what: "Claude Code",
            why: "Add a repository to a group and it is connected there; no registration for every project.",
        });
    else
        steps.push({
            key: "claude",
            ok: false,
            auto: false,
            what: "Connect Claude Code",
            why: `The claude command was not found. Run once in a terminal: ${cl.command}`,
        });
    if (!instances.value.length)
        steps.push({
            key: "env",
            ok: false,
            auto: false,
            what: "Add your Graylog",
            why: "Its URL and an access token of a read-only user.",
        });
    else {
        const missing = instances.value.filter((e) => e.local && !e.secret_set);
        steps.push({
            key: "env",
            ok: !missing.length,
            auto: false,
            what: missing.length
                ? `Add the ${missing.length === 1 ? `token of ${missing[0].name}` : `tokens of ${missing.length} environments`}`
                : plural(instances.value.length, "Graylog environment"),
            why: missing.length
                ? "Saved on this machine only, never in a repository."
                : "With their tokens.",
        });
    }
    return steps;
}
async function waitFor(cond: () => Promise<boolean>, seconds = 180) {
    for (let i = 0; i < seconds / 2; i++) {
        await new Promise((r) => setTimeout(r, 2000));
        try {
            if (await cond()) return true;
        } catch {
            /* restarting */
        }
    }
    return false;
}
export async function setupStep(key: string) {
    if (key === "server") {
        await api("/api/setup/start", "POST", {});
        toast(
            "Starting the server… the first time it installs a copy (about a minute)",
            "info",
        );
        if (
            !(await waitFor(
                async () =>
                    (await api<{ server: { running: boolean } }>("/api/setup"))
                        .server.running,
            ))
        )
            throw new Error(
                `The server did not start; see ${setup.value?.log}`,
            );
    }
    if (key === "autostart")
        await api("/api/setup/autostart", "POST", { on: true });
    if (key === "claude") await api("/api/setup/claude", "POST", {});
    await loadSetup();
    await loadState();
}
export async function finishSetup() {
    let s = setup.value;
    if (!s) return;
    if (!s.server.running) await setupStep("server");
    s = setup.value!;
    if (s.autostart.kind && !s.autostart.enabled) await setupStep("autostart");
    if (
        s.claude.cli &&
        [
            ...s.claude.stdio_projects,
            ...s.claude.stale_projects,
            ...s.claude.stale_files,
            ...s.claude.missing_projects,
        ].length
    )
        await setupStep("claude");
    await loadSetup();
    await loadState();
    const missing = instances.value.find((e) => !e.secret_set && e.local);
    if (!instances.value.length) openEnv(null);
    else if (missing) openEnv(missing);
    else toast("Setup finished");
}
export async function checkUpdates() {
    await loadSetup(true);
    const s = setup.value;
    toast(
        s?.update?.available
            ? "A newer version is available"
            : s?.update?.latest
              ? "You have the latest version"
              : "Could not reach GitHub",
        "info",
    );
}
export async function autostartOff() {
    await api("/api/setup/autostart", "POST", { on: false });
    toast("Will not start at login");
    await loadSetup();
}
export async function setupUpdate() {
    const old = setup.value?.server.commit;
    await api("/api/setup/update", "POST", {});
    toast("Updating… this page reloads when the new version runs", "info");
    const root = base().replace(/\/admin$/, "");
    if (
        await waitFor(async () => {
            const h = await (
                await fetch(`${root}/healthz`, { cache: "no-store" })
            ).json();
            return h.status === "ok" && h.commit !== old;
        })
    )
        location.reload();
    else throw new Error(`The update did not finish; see ${setup.value?.log}`);
}
