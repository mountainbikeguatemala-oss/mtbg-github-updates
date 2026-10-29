/**
 * Settings > Updates: one page for every product's GitHub updates. A section per source (its packages, its modules,
 * what is new on GitHub), the store's setup and the take-over from the earlier updaters, the history with its
 * rollbacks. Every button asks first, saying what will happen (the server answers mtbg_plan / the previews).
 */
import { Component, onWillStart, status, useState } from "@odoo/owl";
import { browser } from "@web/core/browser/browser";
import { ConfirmationDialog } from "@web/core/confirmation_dialog/confirmation_dialog";
import { Dialog } from "@web/core/dialog/dialog";
import { deserializeDateTime, formatDateTime } from "@web/core/l10n/dates";
import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

const SOURCE = "mtbg.github.source";
const BATCH = "mtbg.github.batch";
const UPDATE = "mtbg.github.update";
const TAB_KEY = "mtbg_github_updates_tab"; // the tab, remembered in this browser

function when(value) {
    return value ? formatDateTime(deserializeDateTime(value)) : _t("never");
}

/** A version number clicked: copied to the clipboard (owner, 2026-10-28). */
async function copyVersion(notification, version) {
    if (!version) {
        return;
    }
    try {
        await browser.navigator.clipboard.writeText(version);
        notification.add(_t("Copied: %s", version), { type: "success" });
    } catch {
        notification.add(_t("Could not copy %s", version), { type: "warning" });
    }
}

/** A batch's confirmation: the extensions it brings (and why) with their release notes, or why it cannot run. */
export class PlanDialog extends Component {
    static template = "mtbg_github_updates.PlanDialog";
    static components = { Dialog };
    static props = ["close", "title", "plan", "confirm"];

    setup() {
        this.notification = useService("notification");
    }

    copy(version) {
        return copyVersion(this.notification, version);
    }

    async go() {
        this.props.close();
        await this.props.confirm();
    }
}

/** Roll back an update: what goes back, the backup's time, the data lost, the database's name typed to confirm. */
export class RollbackDialog extends Component {
    static template = "mtbg_github_updates.RollbackDialog";
    static components = { Dialog };
    static props = ["close", "preview", "confirm"];

    setup() {
        this.state = useState({ typed: "" });
        this.when = when;
    }

    get ready() {
        return !this.props.preview.problems.length && this.state.typed.trim() === this.props.preview.database;
    }

    async go() {
        const typed = this.state.typed.trim();
        this.props.close();
        await this.props.confirm(typed);
    }
}

export class MtbgGithubUpdatesPage extends Component {
    static template = "mtbg_github_updates.UpdatesPage";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.notification = useService("notification");
        this.dialog = useService("dialog");
        this.copy = (version) => copyVersion(this.notification, version);
        let tab = "releases";
        try {
            tab = browser.localStorage.getItem(TAB_KEY) || "releases";
        } catch {
            // private windows
        }
        // notes: an extension's notes folded / unfolded by hand; allNotes: the "all notes" toggle (null: the default,
        // open for the extensions of the last update only: owner, 2026-10-28)
        this.state = useState({ data: null, busy: false, tab, open: {}, key: null, history: null, notes: {},
                                allNotes: null });
        this.when = when;
        onWillStart(() => this.load());
    }

    async load() {
        if (status(this) === "destroyed") {
            return;
        }
        this.state.data = await this.orm.call(SOURCE, "mtbg_updates_page", [], { patterns: this.patterns });
    }

    /** A family of extensions only (action.params: modules = name patterns, title), e.g. from the Logistics page. */
    get patterns() {
        return this.props.action?.params?.modules || null;
    }

    get familyTitle() {
        return this.props.action?.params?.title || "";
    }

    showAll() {
        return this.action.doAction("mtbg_github_updates.action_mtbg_github_updates");
    }

    setTab(tab) {
        this.state.tab = tab;
        try {
            browser.localStorage.setItem(TAB_KEY, tab);
        } catch {
            // see setup
        }
    }

    toggle(key) {
        this.state.open[key] = !this.state.open[key];
    }

    /** An extension's release notes: unfolded by hand, else as the "all notes" toggle, else only if it was in the
     * last update. */
    notesShown(module) {
        if (module.id in this.state.notes) {
            return this.state.notes[module.id];
        }
        return this.state.allNotes ?? !!module.in_last_batch;
    }

    toggleNotes(module) {
        this.state.notes[module.id] = !this.notesShown(module);
    }

    get anyNotesShown() {
        return (this.state.data?.sections || []).some((s) => s.modules.some((m) => m.lines?.length && this.notesShown(m)));
    }

    /** All notes on, or all off (each extension's own toggle starts again from there). */
    toggleAllNotes() {
        this.state.allNotes = !this.anyNotesShown;
        this.state.notes = {};
    }

    get updateCount() {
        return this.state.data.sections.reduce((n, s) => n + s.modules.filter((m) => m.update).length, 0);
    }

    /** A server button: a notification, or an action (the progress screen). */
    async run(model, method, args) {
        if (this.state.busy) {
            return;
        }
        this.state.busy = true;
        try {
            const result = await this.orm.call(model, method, args);
            if (result?.tag === "display_notification") {
                const { message, type, sticky } = result.params;
                this.notification.add(message, { type, sticky });
            } else if (result && result.type) {
                await this.action.doAction(result);
                return;
            }
            await this.load();
        } finally {
            this.state.busy = false;
        }
    }

    // ------------------------------------------------------------------ sources
    check() {
        return this.run(SOURCE, "action_check_updates", [[]]);
    }

    testConnection(section) {
        return this.run(SOURCE, "action_test_connection", [[section.id]]);
    }

    createKey(section) {
        return this.run(SOURCE, "action_generate_key", [[section.id]]);
    }

    async saveToken(section, ev) {
        const input = ev.target.closest(".input-group").querySelector("input");
        await this.orm.call(SOURCE, "mtbg_set_token", [[section.id], input.value]);
        input.value = "";
        this.notification.add(_t("Token saved on the server."), { type: "success" });
        await this.load();
    }

    async addKnown(known) {
        await this.orm.call(SOURCE, "mtbg_add_known", [known.key]);
        this.notification.add(_t("%s added: give it its deploy key (Access), then Check GitHub.", known.name), { type: "success" });
        await this.load();
    }

    async copyKey(section) {
        await browser.navigator.clipboard.writeText(section.public_key);
        this.notification.add(_t("Public key copied."), { type: "success" });
    }

    editSource(section) {
        return this.action.doAction({ type: "ir.actions.act_window", res_model: SOURCE, res_id: section.id,
                                      views: [[false, "form"]], target: "new" }, { onClose: () => this.load() });
    }

    // ------------------------------------------------------------------ updates
    async ask(kind, ids, title) {
        const plan = await this.orm.call(SOURCE, "mtbg_plan", [kind, ids]);
        this.dialog.add(PlanDialog, { title, plan, confirm: () => this.run(SOURCE, "mtbg_start", [kind, ids]) });
    }

    update(module) {
        return this.ask("modules", [module.id], _t("Update %s?", module.title));
    }

    updateAll() {
        if (this.patterns) {
            const ids = this.state.data.sections.flatMap((s) => s.modules.filter((m) => m.update).map((m) => m.id));
            return this.ask("modules", ids, _t("Update %s?", this.familyTitle || this.patterns.join(", ")));
        }
        return this.ask("all", [], _t("Update every extension with a newer version?"));
    }

    install(item) {
        return this.ask("install", [item.id], _t("Install %s?", item.title));
    }

    installPackage(pkg) {
        return this.ask("package", [pkg.id], _t("Install the release “%s”?", pkg.name));
    }

    async goBack(module) {
        const preview = await this.orm.call(UPDATE, "mtbg_revert_preview", [module.name]);
        if (!preview.ok) {
            this.dialog.add(ConfirmationDialog, {
                title: _t("Go back (code only): not possible"),
                body: (preview.reasons || [preview.reason]).join("\n") + "\n\n" +
                    _t("The update can still be rolled back with its database, from the history."),
            });
            return;
        }
        this.dialog.add(ConfirmationDialog, {
            title: _t("Go back to %(version)s?", { version: preview.to }),
            body: _t("%(module)s goes back from %(from)s to %(to)s: the code only, the data stays. A backup is taken " +
                     "first; the site restarts (about half a minute without access).",
                     { module: preview.module, from: preview.from, to: preview.to }),
            confirmLabel: _t("Go back"),
            confirm: () => this.run(UPDATE, "mtbg_revert", [module.name]),
            cancel: () => {},
        });
    }

    // ------------------------------------------------------------------ take over, history, rollback
    takeOver() {
        const adopt = this.state.data.adopt;
        this.dialog.add(ConfirmationDialog, {
            title: _t("Take over from the earlier updaters?"),
            body: _t("%(count)s versions in use are copied into this store as they are (nothing is reinstalled, the " +
                     "database is not changed), with their sources, deploy keys and history; the site restarts. " +
                     "Their stores stay as they were.", { count: adopt.todo }),
            confirmLabel: _t("Take over"),
            confirm: () => this.run(BATCH, "mtbg_adopt", []),
            cancel: () => {},
        });
    }

    async rollBack(batch) {
        const preview = await this.orm.call(BATCH, "mtbg_rollback_preview", [[batch.id]]);
        this.dialog.add(RollbackDialog, {
            preview,
            confirm: (typed) => this.run(BATCH, "mtbg_rollback", [[batch.id], typed]),
        });
    }

    allHistory() {
        return this.action.doAction("mtbg_github_updates.action_mtbg_github_update_log");
    }

    stateClass(state) {
        return { applied: "text-bg-success", done: "text-bg-success", restarting: "text-bg-info",
                 restoring: "text-bg-info", failed: "text-bg-danger", undone: "text-bg-secondary",
                 rolled_back: "text-bg-warning", cancelled: "text-bg-secondary" }[state] || "text-bg-light";
    }

    stateLabel(state) {
        return { applied: _t("Done"), done: _t("Done"), restarting: _t("Running"), restoring: _t("Restoring"),
                 failed: _t("Failed"), undone: _t("Rolled back"), empty: "" }[state] || state;
    }

    whatLabel(what) {
        return { update: _t("update"), same: _t("up to date"), "not here": _t("not on this site") }[what] || what;
    }
}

registry.category("actions").add("mtbg_github_updates", MtbgGithubUpdatesPage);
