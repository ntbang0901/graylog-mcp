import { useEffect, useRef } from "preact/hooks";
import { notifications } from "../store";
import { toastIn, toastOut } from "../scripts/motion";
import { Icon, type IconName } from "./UI";
function Toast({
    item,
}: {
    item: { id: number; message: string; kind: string };
}) {
    const ref = useRef<HTMLDivElement>(null);
    useEffect(() => {
        if (ref.current) toastIn(ref.current);
        const timer = setTimeout(
            async () => {
                if (ref.current) await toastOut(ref.current);
                notifications.value = notifications.value.filter(
                    (n) => n.id !== item.id,
                );
            },
            item.kind === "fail" ? 7000 : 3500,
        );
        return () => clearTimeout(timer);
    }, []);
    return (
        <div ref={ref} class={`toast ${item.kind}`}>
            <Icon
                name={
                    (["ok", "fail", "info", "warn"].includes(item.kind)
                        ? item.kind
                        : "ok") as IconName
                }
            />
            <span>{item.message}</span>
        </div>
    );
}
export function Toasts() {
    return (
        <div id="toasts" aria-live="polite">
            {notifications.value.map((n) => (
                <Toast key={n.id} item={n} />
            ))}
        </div>
    );
}
