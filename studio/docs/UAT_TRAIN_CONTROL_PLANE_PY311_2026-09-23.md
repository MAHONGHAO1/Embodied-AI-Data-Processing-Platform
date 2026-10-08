# UAT training control-plane Python 3.11 repair

Date: 2026-09-23

This runbook records the UAT-only repair for the embedded QuicTrain control
plane. It does not change the production service, the legacy `quicdata-uat`
18080 stack, or the standalone `train.quicrobot.xyz` service. No catalog export
was registered and no training job was submitted during the repair.

## Failure and scope

The canonical Studio UAT service is `studio-uat.quicrobot.xyz`, backed by the
source checkout at `/opt/quic_studio/uat/quic_studio` and API port 18081. Its
original API venv was Python 3.10.12. The embedded train mount failed because
the train package imports `datetime.UTC`, which requires Python 3.11. The
catalog bridge therefore returned HTTP 503 with `训练控制面不可用` while the
regular Studio `/health` endpoint remained ready.

## Applied UAT change

An isolated Python 3.11.16 environment was created at:

```text
/opt/quic_studio/uat/quic_studio/backend/.venv311
```

It was provisioned by the host's `uv` using the repository's locked core
dependencies. The cloud training extras were intentionally not enabled for
this registration/view checkpoint; the embedded provider is explicitly fake,
provider submission is disabled, and no scheduler is running.

The API-only systemd drop-in is:

```text
/etc/systemd/system/quicstudio-uat-source@api.service.d/python311.conf
```

It overlays `.venv311` onto the wrapper's `.venv` path only inside the API
service mount namespace. The wrapper and all worker units are unchanged. The
drop-in also pins this checkpoint to the isolated UAT test project and local
artifact directory:

```ini
BindReadOnlyPaths=/opt/quic_studio/uat/quic_studio/backend/.venv311:/opt/quic_studio/uat/quic_studio/backend/.venv
Environment=QUICTRAIN_ENV=production
Environment=QUICTRAIN_AUTH_MODE=studio
Environment=QUICTRAIN_PROVIDER=fake
Environment=QUICTRAIN_PROVIDER_DISABLED=true
Environment=QUICTRAIN_DEFAULT_PROJECT_ID=prj_other
Environment=QUICTRAIN_EMBEDDED_SCHEDULER=0
Environment=QUICTRAIN_ARTIFACT_ROOT=/opt/quic_studio/uat/quic_studio/deploy/uat/runtime/quictrain-artifacts
```

The overlay is reversible without changing the source checkout:

```bash
rm /etc/systemd/system/quicstudio-uat-source@api.service.d/python311.conf
systemctl daemon-reload
systemctl restart quicstudio-uat-source@api.service
```

That rollback restores the original 3.10 API behavior, where `/api/train`
does not mount. The 3.10 worker services are intentionally retained.

## Evidence

Observed after the restart:

- API process runtime: Python 3.11.16.
- `quicstudio-uat-source@worker-control.service`: Python 3.10.12.
- `quicstudio-uat-source@worker-analytics.service`: Python 3.10.12.
- `GET /api/train/health`: HTTP 200, `environment=production`,
  `provider=fake`, `scheduler=external`, and the UAT-local artifact root.
- Authenticated `GET /api/train/api/v1/ops/status` reported
  `capacity.provider_disabled=true`. The existing provider guard rejects job
  submission while leaving health, project reads, and catalog registration
  available.
- `GET /health`: `ready=true`; database, Redis, Celery, QRDF, and storage are
  ready. Storage reports the UAT buckets `quicstudio-uat-raw`,
  `quicstudio-uat-process`, and `quicstudio-uat-export`.
- A real UAT admin JWT was accepted by
  `/api/train/api/v1/auth/me`; it returned `project_id=prj_other`,
  `project_role=admin`, and `auth_mode=studio`. An unauthenticated request
  returned HTTP 401.
- UAT Postgres is the `quicstudio-uat-postgres-1` container and database
  `quicdata`; UAT Redis uses the `quicstudio-uat-redis-state-1` and
  `quicstudio-uat-redis-broker-1` containers. No production host or standalone
  train endpoint was used.
- Before and after the API restart, the training tables reported `jobs=0` and
  `attempts=0`. No training task was created.

## Seed-data cleanup (completed after code repair)

Before cleanup, the UAT training tables contained four repository `SEED_DATASETS` rows
(`dsv_assembly_v4`, `dsv_fold_v8`, `dsv_kitchen_v17`, and
`dsv_pusht_smoke_v1`) under `prj_robot_arm`, including example `oss://` URIs.
They were not used as validation data. After `ef74e67` disabled default sample
seeding, all four were backed up, checked against the complete pre-fix seed
manifests and original creation time, and removed in a guarded transaction
only after confirming there were no training, materialization, or registration
audit references. They did not reappear after restart. Models, resource
profiles, projects, and other records were preserved. The API default is the existing isolated
UAT project `prj_other` (`隔离测试项目`). The initial setup therefore serves
reads and catalog registration through that project; it does not use the
seeded `prj_robot_arm` rows.

## Completed registration check

The UI generated new LeRobot export `6` from immutable catalog version `2`,
registered its verified real metadata as `REGISTERED`, and displayed the
result. ACT validation rejected the EGO sample's absent action/state features;
no job was created. Full evidence and temporary identity cleanup are recorded
in [the registration report](FRONTEND_UAT_TRAIN_REGISTRATION_2026-09-23.md).
If actual training is later enabled, configure an
actual provider, its dependencies, credentials, and scheduler explicitly; the
current `fake` provider and `provider_disabled=true` settings prevent job
execution.
