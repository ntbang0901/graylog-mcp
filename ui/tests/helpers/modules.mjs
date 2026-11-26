import { after } from "node:test";
import { mkdtemp, readFile, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import ts from "typescript";

// Load real UI modules without a browser or a bundler; resolve npm imports from ui/.
export async function loadModules(names) {
    const output = await mkdtemp(join(tmpdir(), "graylog-admin-modules-"));
    after(() => rm(output, { recursive: true, force: true }));
    for (const name of names) {
        const source = await readFile(
            new URL(`../../src/${name}.ts`, import.meta.url),
            "utf8",
        );
        const { outputText } = ts.transpileModule(source, {
            compilerOptions: {
                module: ts.ModuleKind.ESNext,
                target: ts.ScriptTarget.ES2022,
            },
        });
        const javascript = outputText.replace(
            /from "([^"]+)"/g,
            (_, spec) =>
                `from ${JSON.stringify(spec.startsWith(".") ? `${spec}.mjs` : import.meta.resolve(spec))}`,
        );
        await writeFile(join(output, `${name}.mjs`), javascript);
    }
    return Object.fromEntries(
        await Promise.all(
            names.map(async (name) => [
                name,
                await import(pathToFileURL(join(output, `${name}.mjs`)).href),
            ]),
        ),
    );
}
