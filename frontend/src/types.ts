export type Status = "queued" | "running" | "completed" | "failed";

export type TribeHealth = {
  installed: boolean;
  model_id: string;
  device: string;
  cache_dir: string;
  hf_token_present: boolean;
  loaded: boolean;
};

export type LlmReportHealth = {
  model_id: string;
  provider: string;
  hf_token_present: boolean;
};

export type RemoteTribeHealth = {
  configured: boolean;
  url?: string | null;
  token_present: boolean;
  timeout_seconds: number;
};

export type RemoteOcrHealth = {
  configured: boolean;
  url?: string | null;
  token_present: boolean;
  timeout_seconds: number;
};

export type Health = {
  ok: boolean;
  tribev2: TribeHealth;
  llm_report: LlmReportHealth;
  remote_tribe?: RemoteTribeHealth;
  remote_ocr?: RemoteOcrHealth;
};

export type Region = {
  name: string;
  raw: number;
  score: number;
};

export type NetworkScore = {
  label: string;
  raw: number;
  score: number;
};

export type TemporalPoint = {
  index: number;
  start: number;
  duration: number;
  mean_abs: number;
  peak_abs: number;
};

export type AnalysisSummary = {
  created_at: string;
  model: string;
  mesh: string;
  roi_method: string;
  virality_potential?: number;
  metrics: Record<string, number>;
  top_regions: Region[];
  networks: Record<string, NetworkScore>;
  temporal_series: TemporalPoint[];
  surface?: {
    n_vertices: number;
    sample_indices: number[];
    values: number[];
    max: number;
  };
  warnings: string[];
};

export type CalibratedPrediction = {
  predicted_likes: number;
  prediction_low?: number;
  prediction_high?: number;
  prediction_low_wide?: number;
  prediction_high_wide?: number;
  confidence: number;
  sample_count: number;
  model_version?: string;
  prediction_target?: string;
  rank_score?: number;
  ranking_value?: number;
  r2_training: number | null;
  r2_validation?: number | null;
  mae_validation?: number | null;
  log_mae_validation?: number | null;
  wape_validation?: number | null;
  spearman_validation?: number | null;
  validation_strategy?: string | null;
  validation_count?: number | null;
  probability_above_median?: number;
  probability_above_p75?: number;
  probability_above_p90?: number;
};

export type LlmReport = {
  generated_at: string;
  model: string;
  provider: string;
  report: string;
};

export type Post = {
  id: number;
  section: "single" | "historical" | "ab";
  title: string;
  caption?: string | null;
  published_at?: string | null;
  likes?: number | null;
  person_label?: string | null;
  company_label?: string | null;
  post_type_label?: string | null;
  source_ref?: string | null;
  source_row_number?: number | null;
  shortcode?: string | null;
  image_url?: string | null;
  video_url?: string | null;
  analysis_url?: string | null;
  tags?: string[] | null;
  hook_text?: string | null;
  is_animated?: boolean | null;
  comments?: number | null;
  brain_global_mean_abs?: number | null;
  brain_global_peak_abs?: number | null;
  virality_potential?: number | null;
  status: Status;
  error?: string | null;
  progress_percent?: number | null;
  progress_message?: string | null;
  created_at?: string;
  updated_at?: string;
  analysis_summary?: AnalysisSummary | null;
  has_analysis_summary?: boolean | null;
  llm_report?: LlmReport | null;
  calibrated_prediction?: CalibratedPrediction | null;
  tribe_percentile?: number | null;
  rank?: number;
  ranking_basis?: "advanced_prediction" | "calibrated_likes" | "tribev2_global_activation";
  ranking_value?: number;
  is_winner?: boolean;
  label?: string;
};

export type Calibration = {
  ready: boolean;
  sample_count: number;
  feature_order: string[];
  r2_training?: number | null;
  r2_validation?: number | null;
  mae_training?: number | null;
  mae_validation?: number | null;
  wape_validation?: number | null;
  validation_strategy?: string | null;
  blend_alpha?: number | null;
  knn_k?: number | null;
  train_median_likes?: number | null;
  train_p75_likes?: number | null;
  train_p95_likes?: number | null;
  message?: string | null;
};

export type MetadataOptions = {
  people: string[];
  companies: string[];
  post_types: string[];
  tags: string[];
};

export type AbTest = {
  id: number;
  name: string;
  status: "running" | "completed" | "failed";
  winner_post_id?: number | null;
  created_at: string;
  updated_at: string;
};
