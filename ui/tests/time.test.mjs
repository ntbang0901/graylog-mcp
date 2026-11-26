import { test } from "node:test";
import assert from "node:assert/strict";
import { loadModules } from "./helpers/modules.mjs";

const { time, explorer } = await loadModules(["time", "explorer", "router"]);
function inZone(zone, run) {
    const before = process.env.TZ;
    process.env.TZ = zone;
    try {
        run();
    } finally {
        if (before === undefined) delete process.env.TZ;
        else process.env.TZ = before;
    }
}

test("API timestamps with UTC, offsets, epoch milliseconds or no zone show the same browser time", () => {
    inZone("Asia/Ho_Chi_Minh", () => {
        const instant = "2026-11-18T17:12:48.123Z";
        for (const value of [
            instant,
            "2026-11-18 12:12:48.123-05:00",
            "2026-11-18 17:12:48.123",
            "2026-11-18T17:12:48.123",
            Date.parse(instant),
            new Date(instant),
        ]) {
            assert.equal(
                time.formatTime(value, "YYYY-MM-DD HH:mm:ss.SSS"),
                "2026-11-19 00:12:48.123",
            );
        }
        assert.equal(
            time.formatDateTime(instant),
            "2026-11-19 00:12:48 UTC+07:00",
        );
        assert.equal(time.parseTime(0).valueOf(), 0);
    });
});

test("invalid and missing timestamps stay empty rather than showing now", () => {
    for (const value of [
        null,
        undefined,
        "",
        "  ",
        "bad timestamp",
        NaN,
        Infinity,
        new Date(NaN),
    ]) {
        assert.equal(time.parseTime(value), null);
        assert.equal(time.formatTime(value), "-");
        assert.equal(time.localInput(value), "");
    }
    assert.equal(time.zoned("bad timestamp"), null);
    assert.equal(
        time.zoned("2026-11-18T00:00:00Z", "Nowhere/Land").zone,
        "UTC",
    );
});

test("saved chart labels only convert when they identify an absolute instant", () => {
    inZone("America/New_York", () => {
        assert.equal(
            time.absoluteTime("2026-11-01T05:00:00.000Z").format("HH:mm Z"),
            "01:00 -04:00",
        );
        assert.equal(
            time.absoluteTime("2026-11-01T06:00:00.000Z").format("HH:mm Z"),
            "01:00 -05:00",
        );
        assert.equal(time.absoluteTime("01:00"), null);
        assert.equal(time.absoluteTime("2026-11-01 01:00"), null);
        assert.equal(time.absoluteTime("invalid"), null);
    });
});

test("custom ranges convert between the browser wall clock and UTC", () => {
    inZone("Asia/Ho_Chi_Minh", () => {
        assert.equal(
            time.localInput("2026-11-18T17:12:48.123Z"),
            "2026-11-19T00:12",
        );
        assert.equal(
            time.localInputToUtc("2026-11-19T00:12"),
            "2026-11-18T17:12:00.000Z",
        );
        for (const input of [
            "",
            "bad",
            "2026-02-30T12:00",
            "2026-11-19T24:12",
        ]) {
            assert.equal(time.localInputToUtc(input), null);
        }
    });
    inZone("Asia/Kathmandu", () => {
        assert.equal(
            time.localInput("2026-11-18T00:00:00Z"),
            "2026-11-18T05:45",
        );
        assert.equal(
            time.localInputToUtc("2026-11-18T05:45"),
            "2026-11-18T00:00:00.000Z",
        );
    });
});

test("daylight saving changes keep their actual offsets and skipped input times are rejected", () => {
    inZone("America/New_York", () => {
        assert.equal(
            time.formatDateTime("2026-03-08T06:59:00Z"),
            "2026-03-08 01:59:00 UTC-05:00",
        );
        assert.equal(
            time.formatDateTime("2026-03-08T07:00:00Z"),
            "2026-03-08 03:00:00 UTC-04:00",
        );
        assert.equal(time.localInputToUtc("2026-03-08T02:30"), null);
        assert.equal(
            time.localInputToUtc("2026-03-08T03:30"),
            "2026-03-08T07:30:00.000Z",
        );
        assert.equal(
            time.formatDateTime("2026-11-01T05:30:00Z"),
            "2026-11-01 01:30:00 UTC-04:00",
        );
        assert.equal(
            time.formatDateTime("2026-11-01T06:30:00Z"),
            "2026-11-01 01:30:00 UTC-05:00",
        );
    });
});

test("short call timestamps compare browser calendar days and use a 24 hour clock at midnight", () => {
    inZone("Asia/Ho_Chi_Minh", () => {
        const now = Date.parse("2026-11-18T17:10:00Z");
        assert.equal(
            time.whenShort(Date.parse("2026-11-18T17:00:00Z") / 1000, now),
            "00:00:00",
        );
        assert.equal(
            time.whenShort(Date.parse("2026-11-18T16:59:00Z") / 1000, now),
            "Wed 23:59",
        );
    });
});

test("explorer ranges keep their exact instants when the server and browser use different zones", () => {
    inZone("America/New_York", () => {
        const view = {
            from: "2026-11-18T00:00:00Z",
            to: "2026-11-18T01:00:00Z",
        };
        const range = explorer.windowOf(
            {
                range: {
                    from: "2026-11-18 09:00:00.123+07:00",
                    to: "2026-11-18 09:15:00.456+07:00",
                },
            },
            view,
        );
        assert.equal(range[0].toISOString(), "2026-11-18T02:00:00.123Z");
        assert.equal(range[1].toISOString(), "2026-11-18T02:15:00.456Z");
        assert.equal(explorer.localInput(range[0]), "2026-11-17T21:00");
        assert.equal(
            explorer
                .windowOf({ range: { from: "bad", to: "bad" } }, view)[0]
                .toISOString(),
            view.from.replace("Z", ".000Z"),
        );
        assert.equal(explorer.windowOf(null, { from: "bad", to: "bad" }), null);
    });
});
