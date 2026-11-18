import { density, mode, palette, settingsOpen } from "../store";
import {
    SCHEMES,
    originOf,
    setDensity,
    setMode,
    setPalette,
} from "../appearance";
import { bump } from "../scripts/motion";
import { Close, Dialog, Segment, modKey } from "./UI";
export function Settings() {
    return (
        <Dialog
            id="settings"
            aria-labelledby="setTitle"
            open={settingsOpen.value}
            onClose={() => {
                settingsOpen.value = false;
            }}
        >
            <div class="dhead">
                <h2 id="setTitle">Settings</h2>
                <span class="spacer" />
                <Close
                    onClick={() => {
                        settingsOpen.value = false;
                    }}
                />
            </div>
            <div class="dbody">
                <div>
                    <div class="glabel gap">Colour scheme</div>
                    <div
                        class="schemes"
                        role="radiogroup"
                        aria-label="Colour scheme"
                        id="schemes"
                        onKeyDown={(e) => {
                            const dirs: Record<string, number> = {
                                    ArrowRight: 1,
                                    ArrowDown: 1,
                                    ArrowLeft: -1,
                                    ArrowUp: -1,
                                },
                                d = dirs[e.key];
                            if (!d) return;
                            e.preventDefault();
                            const n =
                                SCHEMES[
                                    (SCHEMES.findIndex(
                                        (s) => s.id === palette.value,
                                    ) +
                                        d +
                                        SCHEMES.length) %
                                        SCHEMES.length
                                ].id;
                            setPalette(n);
                            e.currentTarget
                                .querySelector<HTMLButtonElement>(
                                    `[data-scheme="${n}"]`,
                                )
                                ?.focus();
                        }}
                    >
                        {SCHEMES.map((s) => (
                            <button
                                class="scheme"
                                role="radio"
                                data-scheme={s.id}
                                aria-checked={palette.value === s.id}
                                onClick={(e) => {
                                    setPalette(s.id);
                                    bump(e.currentTarget);
                                }}
                            >
                                <span class="pv" aria-hidden="true">
                                    {["light", "dark"].map((t) => (
                                        <div data-palette={s.id} data-theme={t}>
                                            <i class="a" />
                                            <i class="s" />
                                            <i class="s short" />
                                            <i class="f" />
                                        </div>
                                    ))}
                                </span>
                                <span class="nm">
                                    {s.name}
                                    {s.def && (
                                        <span class="glabel">default</span>
                                    )}
                                </span>
                                <span class="ds">{s.desc}</span>
                            </button>
                        ))}
                    </div>
                </div>
                <div class="srow">
                    <div class="glabel">Appearance</div>
                    <Segment
                        id="modeSeg"
                        label="Appearance"
                        options={[
                            ["system", "System"],
                            ["light", "Light"],
                            ["dark", "Dark"],
                        ]}
                        value={mode.value}
                        onChange={(v) =>
                            setMode(v, originOf(document.activeElement))
                        }
                    />
                </div>
                <div class="srow">
                    <div class="glabel">Density</div>
                    <Segment
                        id="densSeg"
                        label="Density"
                        options={[
                            ["comfortable", "Comfortable"],
                            ["compact", "Compact"],
                        ]}
                        value={density.value}
                        onChange={setDensity}
                    />
                </div>
                <div>
                    <div class="glabel gap">Shortcuts</div>
                    <div class="keys">
                        {[
                            ["Command palette", [`${modKey()} K`, "/"]],
                            ["Environment", ["E"]],
                            ["Settings", [","]],
                            ["Light / dark", ["T"]],
                            ["Scheme", ["1", "2", "3", "4"]],
                            ["Go to screen", ["G", "O A E F R P C"]],
                        ].map(([name, keys]) => (
                            <div>
                                <span>{name}</span>
                                <span>
                                    {(keys as string[]).map((k) => (
                                        <span class="kbd">{k}</span>
                                    ))}
                                </span>
                            </div>
                        ))}
                    </div>
                </div>
            </div>
        </Dialog>
    );
}
