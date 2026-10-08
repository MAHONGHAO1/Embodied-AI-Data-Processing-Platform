# Frontend performance follow-up — 2026-09-23

## Scope and method

This is a static review of the current frontend after the assets/datasets and mining-console template migrations. The QRDF retirement pass measured `frontend/js/app.js` at 12,683 lines and 828,199 bytes at `HEAD`, then 11,842 lines and 778,280 bytes after cleanup (−841 lines, −49,919 bytes). The matching stylesheet moved from 984 lines and 92,866 bytes to 857 lines and 78,563 bytes (−127 lines, −14,303 bytes), and the eager `dataset-revision-builder.js` script was removed from `frontend/index.html`. Later shared working-tree edits may change final checkout totals. The checks compare setup declarations, function names, return-object exposure, template references, and every repository reference to the retired entrypoint.

`app.js` is still eagerly downloaded and parsed by `frontend/index.html`. `page-components.js` now defers six migrated pages, but it does not defer the app setup, global template, translations, old dialog markup, or the existing eager helpers. The six page components are small enough to remain route-owned, but `page-components.js` deliberately keeps loaded script/link promises in its cache, so a visited page remains resident for the session.

## Handled: retired catalog compatibility and assetDataset builder

The following coordinated slice had no current template reference and included a backend route that is permanently retired:

- `frontend/js/app.js:1247-1254` — `dataAssets`, `assetSourceWorkspaceId`, `catalogDatasets`, `catalogVersionsByDataset`, `catalogExportByVersion`, `selectedCatalogDatasetId`, and the `lerobotCatalogImportForm`/dialog state.
- `frontend/js/app.js:1741-1832` — `loadDataAssets`, `loadCatalogDatasets`, `loadCatalogVersions`, `exportCatalogVersion`, `catalogRegisterHint`, `registerCatalogToTrain`, and `submitLerobotCatalogImport`.
- the matching return-object entries;
- the `assetDataset*` state, actions, derived rows, and return bindings that were only consumed by the retired asset-to-dataset builder;
- the unreachable global asset-dataset dialog.

This slice is now removed from `frontend/js/app.js` as one unit. On the current checkout the change reduced the file by 9,838 bytes and 200 lines (833,602 → 823,764 bytes; 12,811 → 12,611 lines). `node --check frontend/js/app.js` passes. The permanently retired LeRobot import route has no runtime UI/function binding left in the app; the unrelated translation key is retained for message-table stability. The active native LeRobot batch detail, active intake `overviewBuild*` flow, package workbench, and all settings code were left untouched.

## Handled: orphaned QRDF dataset builder and revision dialog

The current `assets`/`datasets` route renders `<data-catalog>`, so the old QRDF list, revision candidate builder, export job subscriptions, and two global dialogs had no reachable UI. A repository-wide reference audit found no component or imperative caller for the exposed `openDataset*`/revision-builder methods. The cleanup removed the complete app slice:

- QRDF list/revision state, loading flags, filters, paging, route/query plumbing, workspace/session resets, realtime export updates, and return-object bindings;
- the old row/detail/create/export functions and both global dialogs, while retaining the active `nativeLerobot*`, `overviewBuild*`, and package workbench flows;
- `frontend/js/dataset-revision-builder.js`, its eager `frontend/index.html` entry, builder/preview tests, and builder-only stylesheet rules;
- the one active LeRobot request-id caller was moved to a local cryptographically random UUID helper, so native LeRobot no longer depends on the retired builder global.

The standalone catalog tests continue to cover dataset listing, immutable versions, and asset selection. The app no longer references `QuicDataDatasetRevisionBuilder`, `listDatasetRevisionCandidates`, or `createDatasetRevision`.

## Remaining orphaned intake filter/governance UI

The retired asset-to-dataset builder is removed above. Separate legacy intake filters and the governance report still remain in `app.js` after the `intake` route moved to `data-overview`; they are not part of the QRDF catalog cleanup and should be removed only as a coordinated slice:

- `annotationPageTab`, `annotationFilterState`, `datasetFilterState`, their option/filtered-row computeds, and return bindings have no current template consumer;
- `governanceReportVisible`, `governanceReportRow`, `openGovernanceReport`, report-only formatting, and the global governance dialog have no current template action;
- `overviewImportStage`, `overviewImportStageEnabled`, `stageItemCheckCounts`, and related status helpers must stay because `miningStageAggregatedStatus` still consumes them;
- `overviewBuildMode`, `overviewBuildForm`, `submitOverviewBuild`, and the data-overview build flow must stay because the active intake page emits `build` into that flow;
- `batchTagFilterState` and its matching functions are also orphaned; the active `miningTasks` view uses `miningTaskFilter` and `miningBatchDetailFilter`, while the intake view uses `data-overview`'s own filters. Keep the shared stage helpers only while an active status projection still consumes them.

The remaining filters/report can be deleted after confirming the active data-overview and mining views no longer depend on them, while preserving the shared stage helpers and `overviewBuild*` flow.

## Do not delete in this pass

- The old `batches` page is still active and renders native LeRobot batch detail, batch cut drawers, and review-package actions. Preserve its `nativeLerobot*`, `batchCut*`, and package-review state.
- `work-queue` and `workbench` remain active. Their queue/workbench save, cut, annotation, and review helpers are not legacy merely because the new package workbench exists.
- `overviewBuild*`, package drawer/review actions, and the new `data-overview` API path remain active.
- `miningTasks` and the lazy `miningDash` page remain active. Mining cut helpers are still used by batch cut UI. `miningConfig`/`miningCloud`/`miningCuts` route remnants can be isolated later, but deleting their shared cut/state functions now would break active batch interactions.
- Global workspace, task-set, collector/device, admin, settings, train-console, and native LeRobot dialogs remain reachable from current routes.

## Lazy component lifecycle audit

The five lazy pages are correctly generation-guarded:

- `frontend/js/collection-overview.js:18-71` invalidates stale loads and calls `loader.dispose()` on unmount.
- `frontend/js/data-overview.js:20-111` guards page/detail generations and disposes the pager plus detail generation on unmount.
- `frontend/js/intake-review-workbench.js:317-392,658-676,1049-1055` invalidates package/preview responses, removes the `beforeunload` handler, pauses/removes the video source, cancels media state, and disposes autosave/loaders.
- `frontend/js/package-annotation-workbench.js:455-504,736-752,1212-1219` invalidates package/episode responses, pauses/removes the video source, cancels its paint RAF, disposes autosave/loaders, and removes `beforeunload`.
- `frontend/js/data-catalog.js:336-450,613-627,1025-1038` guards every loader, stops the visibility-aware ten-second export poll, removes the visibility listener, and disposes loaders on unmount.

These components do not leave a DOM listener, video source, RAF, or poll timer attached after unmount. Their remaining limitation is that the underlying `QuicDataAPI` promises are not abortable; a slow request stays in flight until the API resolves, while the generation/disposed guards prevent stale state from being applied. This is a bounded network-lifetime issue rather than a visible UI/media leak. `page-components.js` also retains loaded scripts/styles in its warm cache by design; an eventual route-level module system could release code/CSS, but doing so would trade away fast revisit behavior and is not required for this pass.

The catalog list response is a separate payload-size opportunity. Current UAT rows can carry a full `source_snapshot_json`, inline admission report, and all source references (roughly 4 KB for one asset row). The list view only needs identity, workspace/batch labels, status, compact duration, and the fields used for filtering; `getDataAsset` can provide the full frozen submission, intervals, and descriptions after the reviewer opens one asset. Any summary projection must retain exact nanosecond interval or total fields (`effective_duration_ns` and the governed interval data) so a five-second or 19.4-second asset is never displayed as zero through an hours-only fallback. This is a safe API projection/detail split for a later backend change; the frontend should keep using the real detail response for exact duration and descriptions.

## Remaining legacy work and validation order

1. Remove the orphaned global governance/legacy filter UI after splitting the mining stage helpers. Keep `overviewBuild*` and the shared stage counters used by mining status.
2. Re-measure production parse/compile and initial request counts, then consider isolating remaining mining route remnants.
