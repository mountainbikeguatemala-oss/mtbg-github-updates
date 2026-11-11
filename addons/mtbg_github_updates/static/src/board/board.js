/** @odoo-module **/
/* The extensions board (0.4.0): Odoo's kanban, one column per source; each column's header carries the source's own
   actions (Check GitHub, Access, Test connection). Columns of no source: "Not from GitHub". */
import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { kanbanView } from "@web/views/kanban/kanban_view";
import { KanbanHeader } from "@web/views/kanban/kanban_header";
import { KanbanRenderer } from "@web/views/kanban/kanban_renderer";

const SOURCE = "mtbg.github.source";

export class MguBoardHeader extends KanbanHeader {
    static template = "mtbg_github_updates.BoardHeader";

    setup() {
        super.setup();
        this.orm = useService("orm");
        this.actionService = useService("action");
    }

    get mguSourceId() {
        return this.group.groupByField?.name === "mgu_board_source_id" && this.group.value ? this.group.value : false;
    }

    get mguTitle() {
        if (this.group.groupByField?.name === "mgu_board_source_id" && !this.group.value) {
            return _t("Not from GitHub");
        }
        return this.group.displayName;
    }

    async mguRun(method) {
        const result = await this.orm.call(SOURCE, method, [[this.mguSourceId]]);
        if (result && typeof result === "object") {
            await this.actionService.doAction(result, { onClose: () => this.props.list.model.load() });
        }
        await this.props.list.model.load();
    }
}

export class MguBoardRenderer extends KanbanRenderer {
    static components = { ...KanbanRenderer.components, KanbanHeader: MguBoardHeader };
}

registry.category("views").add("mgu_board", { ...kanbanView, Renderer: MguBoardRenderer });
