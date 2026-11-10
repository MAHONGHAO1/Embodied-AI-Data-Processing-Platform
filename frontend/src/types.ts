export type RouteKey = 'overview' | 'episodes' | 'runs' | 'delivery';

export interface EpisodeRecord {
  episode_index: number;
  row_count: number;
  disposition: string;
  quality: { status?: string; issue_count?: number; issues?: Issue[] };
  annotation: { task?: string; outcome?: string; tags?: string[]; notes?: string };
  review: { status?: string; reason?: string };
}

export interface Issue {
  episode_index?: number;
  row_index?: number | null;
  frame_index?: number | null;
  rule?: string;
  severity?: string;
  message?: string;
  evidence?: Record<string, unknown>;
}

export interface Batch {
  batch_id: string;
  label: string;
  revision: number;
  content_version: number;
  source: { kind?: string; repo_id?: string; revision?: string; camera?: string; cameras?: string[]; fps?: number; input_fingerprint?: string; [key: string]: unknown };
  episodes: EpisodeRecord[];
  counts?: Record<string, { episodes: number; rows: number }>;
  total_episodes?: number;
  total_rows?: number;
  cleaning_confirmed?: boolean;
  outputs?: Array<Record<string, unknown> & { output_id: string; current?: boolean; artifact_path?: string }>;
  deliveries?: Array<Record<string, unknown>>;
}

export interface CameraRecord {
  name: string;
  video_url: string;
  from_timestamp: number;
  to_timestamp: number | null;
  available: boolean;
}

export interface EpisodeDetail {
  episode_index: number;
  row_count: number;
  fps: number;
  state_names: string[];
  action_names: string[];
  cameras: CameraRecord[];
  quality: { status?: string; issues?: Issue[]; coverage?: Array<Record<string, unknown>> };
  disposition: string;
  annotation: { task?: string; outcome?: string; tags?: string[]; notes?: string };
  review: { status?: string; reason?: string };
  samples: Array<Record<string, unknown>>;
}

export interface CameraInspection {
  name: string;
  fps: number | null;
  frame_count: number;
  width: number;
  height: number;
  codec: string;
  from_timestamp: number;
  to_timestamp: number | null;
  mapping_claim: string;
}

export interface RunRecord {
  run_id: string;
  kind?: string;
  status: string;
  message?: string;
  batch_id?: string;
  request?: { operation?: string; kind?: string; label?: string };
  nodes?: Array<{ node_id: string; label: string; status: string; message?: string; started_at?: string; completed_at?: string }>;
  business_registration?: { status?: string; message?: string };
}
