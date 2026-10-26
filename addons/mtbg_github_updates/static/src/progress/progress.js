/**
 * After an update, a take-over or a rollback: the server restarts (requests fail while it is down). This screen
 * waits through the restart and shows each extension's result as soon as it is known.
 *
 * Odoo often reloads the whole page after the restart (new assets), and the reloaded action comes back without its
 * params: what is followed is kept in this tab's session storage too (and the server falls back to the batch still
 * running), so the screen picks up where it was instead of waiting forever.
 */
import { Component, onWillStart, onWillUnmount, useState } from "@odoo/owl";
import { browser } from "@web/core/browser/browser";
import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { rpc } from "@web/core/network/rpc";
import { useService } from "@web/core/utils/hooks";

const DONE = ["applied", "failed", "rolled_back", "cancelled"];
const KEY = "mtbg_github_updates_following";

function storage(fn) {
    try {
        return fn(browser.sessionStorage);
    } catch {
        return null; // blocked storage: the server falls back to the batch still running
    }
}

function call(model, method, args) {
    return rpc(`/web/dataset/call_kw/${model}/${method}`, { model, method, args, kwargs: {} }, { silent: true });
}

export class MtbgGithubProgress extends Component {
    static template = "mtbg_github_updates.Progress";
    static props = ["*"];

    setup() {
        this.action = useService("action");
        this.state = useState({ lines: [], offline: false, seconds: 0, done: false, phase: "", restore: null });
        const params = this.props.action.params || {};
        if (params.batch || params.restore) {
            storage((s) => s.setItem(KEY, JSON.stringify({ batch: params.batch || null, restore: params.restore || null })));
        }
        const kept = JSON.parse(storage((s) => s.getItem(KEY)) || "{}");
        this.batch = params.batch || (!params.restore && kept.batch) || null;
        this.restore = params.restore || (!params.batch && kept.restore) || null;
        onWillStart(() => this.poll());
        this.timer = setInterval(() => this.poll(), 2000);
        onWillUnmount(() => clearInterval(this.timer));
    }

    finish() {
        this.state.done = true;
        clearInterval(this.timer);
        storage((s) => s.removeItem(KEY));
    }

    async poll() {
        this.state.seconds += 2;
        try {
            if (this.restore) {
                const restore = await call("mtbg.github.batch", "mtbg_restore_progress", [this.restore]);
                this.state.offline = false;
                this.state.restore = restore;
                if (["done", "done_with_problems", "failed"].includes(restore.state) && this.state.seconds > 6) {
                    this.finish();
                }
                return;
            }
            const lines = await call("mtbg.github.update", "mtbg_progress", [this.batch]);
            this.state.offline = false;
            this.state.lines = lines;
            // back up after the restart: finish the batch (the normal Odoo way, from this request)
            if (lines.some((l) => l.state === "restarting") && !this.applying) {
                this.applying = true;
                try {
                    this.state.phase = await call("mtbg.github.update", "mtbg_apply", [this.batch]);
                } catch {
                    // the upgrade restarts the registry or the server: the next poll tells
                } finally {
                    this.applying = false;
                }
                return;
            }
            const back = lines.filter((l) => l.kind === "rollback");
            const relevant = back.length ? back : lines;
            if (lines.length && relevant.every((l) => DONE.includes(l.state))) {
                this.finish();
            }
        } catch {
            this.state.offline = true; // restarting
        }
    }

    get failed() {
        return this.state.lines.some((l) => l.state === "failed") || this.state.restore?.state === "failed";
    }

    label(state) {
        return {
            restarting: _t("Restarting and upgrading…"), applied: _t("Done"), failed: _t("Failed, switched back"),
            rolled_back: _t("Rolled back"), cancelled: _t("Cancelled"),
        }[state] || state;
    }

    restoreLabel(state) {
        return {
            started: _t("Starting…"), restoring: _t("Restoring the backup into a new database…"),
            swapping: _t("Swapping the databases…"), done: _t("Done"), done_with_problems: _t("Done, with problems"),
            failed: _t("Failed: nothing was changed"), unknown: _t("Restarting…"),
        }[state] || state;
    }

    reload() {
        window.location.reload();
    }

    page() {
        this.action.doAction("mtbg_github_updates.action_mtbg_github_updates");
    }
}

registry.category("actions").add("mtbg_github_progress", MtbgGithubProgress);
