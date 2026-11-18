import { render } from "preact";
import { useEffect } from "preact/hooks";
import { initToken, token, errorText } from "./api";
import {
    banner,
    checks,
    initStore,
    instances,
    loadSetup,
    loadState,
    runDoctor,
} from "./store";
import { Shell } from "./components/Shell";
import { Overview } from "./components/Overview";
import { Activity } from "./components/Activity";
import { Logs } from "./components/Logs";
import { Environments } from "./components/Environments";
import { LogFields } from "./components/LogFields";
import { Redaction } from "./components/Redaction";
import { Playground } from "./components/Playground";
import { Connect } from "./components/Connect";
import { ConfigFile } from "./components/ConfigFile";
import { EnvDrawer } from "./components/EnvDrawer";
import { LogsDrawer } from "./components/LogsDrawer";
import { Palette } from "./components/Palette";
import "./styles/main.css";
function App() {
    useEffect(() => {
        if (!token.value) return;
        void loadState()
            .then(() => {
                void loadSetup();
                if (instances.value.length) void runDoctor();
                else checks.value = [];
            })
            .catch((e) => {
                banner.value = errorText(e);
            });
    }, []);
    return (
        <Shell
            overlays={
                <>
                    <EnvDrawer />
                    <LogsDrawer />
                    <Palette />
                </>
            }
        >
            <Overview />
            <Activity />
            <Logs />
            <Environments />
            <LogFields />
            <Redaction />
            <Playground />
            <Connect />
            <ConfigFile />
        </Shell>
    );
}
function start() {
    initToken();
    initStore();
    render(<App />, document.getElementById("app")!);
}
start();
