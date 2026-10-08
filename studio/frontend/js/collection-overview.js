/* Collection overview shell: three dashboards sharing one filter bar. */
const QuicStudioCollectionOverview = (() => {
  function install(app) {
    const Filters = QuicStudioCollectionDashboardFilters;
    const DataBoard = QuicStudioCollectionDataBoard;
    const CapacityBoard = QuicStudioCollectionCapacityBoard;
    const EfficiencyBoard = QuicStudioCollectionEfficiencyBoard;
    app.component('collection-overview', {
      components: {
        'collection-dashboard-filters': Filters.component,
        'collection-data-board': DataBoard.component,
        'collection-capacity-board': CapacityBoard.component,
        'collection-efficiency-board': EfficiencyBoard.component,
      },
      props: {
        workspaceId: { type: [Number, String], default: null },
        projects: { type: Array, default: () => [] },
        locale: { type: String, default: 'zh-CN' },
      },
      setup(props) {
        const board = Vue.ref('data');
        const filters = Vue.ref(Filters.defaultFilters());
        const tasks = Vue.ref([]);
        const labels = Vue.ref([]);
        let generation = 0;
        const items = (payload) => (Array.isArray(payload?.items) ? payload.items : (Array.isArray(payload) ? payload : []));
        async function loadOptions(workspaceId) {
          const current = ++generation;
          tasks.value = [];
          labels.value = [];
          if (!workspaceId) return;
          const [taskResult, labelResult] = await Promise.allSettled([
            QuicDataAPI.listCollectionTasks(workspaceId),
            QuicDataAPI.listCollectionLabels({ workspace_id: workspaceId }),
          ]);
          if (current !== generation) return;
          tasks.value = taskResult.status === 'fulfilled' ? items(taskResult.value) : [];
          labels.value = labelResult.status === 'fulfilled' ? items(labelResult.value) : [];
        }
        Vue.watch(() => props.workspaceId, (workspaceId) => {
          filters.value = Filters.defaultFilters();
          loadOptions(workspaceId);
        }, { immediate: true });
        const text = (zh, en) => (props.locale === 'en-US' ? en : zh);
        return {
          board,
          filters,
          tasks,
          labels,
          text,
          params: Vue.computed(() => Filters.toParams(filters.value)),
        };
      },
      template: `
        <section class="view-stack" aria-label="Collection overview">
          <div v-if="$slots.scope" class="page-actions collection-scope-actions"><slot name="scope" /></div>
          <el-tabs v-model="board" class="overview-board-tabs">
            <el-tab-pane :label="text('产能看板', 'Capacity dashboard')" name="capacity" />
            <el-tab-pane :label="text('数采看板', 'Collection dashboard')" name="data" />
            <el-tab-pane :label="text('人效看板', 'Efficiency dashboard')" name="people" />
          </el-tabs>
          <collection-dashboard-filters v-model="filters" :projects="projects" :tasks="tasks" :labels="labels" :locale="locale" />
          <collection-capacity-board v-if="board === 'capacity'" :workspace-id="workspaceId" :filters="params" :locale="locale" />
          <collection-data-board v-else-if="board === 'data'" :workspace-id="workspaceId" :filters="params" :locale="locale" />
          <collection-efficiency-board v-else :workspace-id="workspaceId" :filters="params" :locale="locale" />
        </section>`,
    });
  }
  return { install };
})();
