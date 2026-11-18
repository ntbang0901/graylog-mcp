import { confirmation, resolveConfirm } from "../store";
import { Dialog } from "./UI";
export function ConfirmDialog() {
    const c = confirmation.value;
    return (
        <Dialog
            id="modalWrap"
            class="layer-dialog"
            animated
            aria-labelledby="modalTitle"
            aria-describedby="modalText"
            open={!!c}
            onClose={() => resolveConfirm(false)}
        >
            <div class="modal" role="alertdialog">
                <h3 id="modalTitle">{c?.title}</h3>
                <div class="muted" id="modalText">
                    {c?.text}
                </div>
                <div class="actions">
                    <button
                        class="btn"
                        autoFocus={c?.danger}
                        onClick={() => resolveConfirm(false)}
                    >
                        Cancel
                    </button>
                    <button
                        class={c?.danger ? "btn danger-solid" : "btn primary"}
                        id="modalOk"
                        autoFocus={!c?.danger}
                        onClick={() => resolveConfirm(true)}
                    >
                        {c?.okLabel || "Confirm"}
                    </button>
                </div>
            </div>
        </Dialog>
    );
}
