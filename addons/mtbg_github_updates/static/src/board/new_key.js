/** @odoo-module **/
/* The new deploy key in a popup (0.4.2): copied at once where the browser allows it, a Copy button, the steps, and
   GitHub's own "Add deploy key" page for the repository. */
import { Component, onMounted, useState } from "@odoo/owl";
import { browser } from "@web/core/browser/browser";
import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

export class MguNewKey extends Component {
    static template = "mtbg_github_updates.NewKey";
    static props = ["*"];

    setup() {
        this.notification = useService("notification");
        this.actionService = useService("action");
        this.params = this.props.action.params || {};
        this.state = useState({ copied: false });
        onMounted(() => this.copy(true));
    }

    async copy(quiet = false) {
        try {
            await browser.navigator.clipboard.writeText(this.params.key || "");
            this.state.copied = true;
            if (!quiet) {
                this.notification.add(_t("Key copied: paste it on GitHub."), { type: "success" });
            }
        } catch {
            if (!quiet) {
                this.notification.add(_t("The browser did not allow copying: select the key and copy it."), { type: "warning" });
            }
        }
    }

    close() {
        return this.actionService.doAction({ type: "ir.actions.act_window_close" });
    }
}

registry.category("actions").add("mgu_new_key", MguNewKey);
