import { api } from "../api";
import { addContextItem } from "../contexts";
import { logItem, type ContextScope } from "../contexts-model";
import type { ExplorerMessage } from "../explorer";
import { AsyncButton } from "./UI";

export function AddLogToContext({
    m,
    scope,
    loaded = false,
    cutFields = [],
}: {
    m: ExplorerMessage;
    scope: ContextScope;
    loaded?: boolean;
    cutFields?: string[];
}) {
    return (
        <AsyncButton
            class="btn sm ghost"
            run={async () => {
                let message = m;
                const limits: string[] = cutFields.length
                    ? [
                          `Fields shortened by get_message: ${cutFields.join(", ")}`.slice(
                              0,
                              500,
                          ),
                      ]
                    : [];
                if (m.ref && !loaded) {
                    const result = await api<{
                        result: {
                            message: ExplorerMessage;
                            cut_fields?: Record<string, number>;
                        };
                    }>("/api/run", "POST", {
                        tool: "get_message",
                        instance: scope.instance,
                        args: { ref: m.ref },
                    });
                    message = { ...m, ...result.result.message };
                    if (result.result.cut_fields)
                        limits.push(
                            `Fields shortened by get_message: ${Object.keys(result.result.cut_fields).join(", ")}`.slice(
                                0,
                                500,
                            ),
                        );
                }
                addContextItem(logItem(message, scope, limits));
            }}
        >
            Add log to context
        </AsyncButton>
    );
}
