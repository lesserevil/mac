export type ViewKey = "overview" | "agents" | "tasks" | "hermes" | "runtime" | "observability" | "secrets";
export type Tone = "good" | "warn" | "bad" | "info";
export type JsonObject = Record<string, unknown>;

export interface ApiRecord {
  id: string;
  [key: string]: unknown;
}

export interface AgentRecord extends ApiRecord {
  name: string;
  machine_id: string;
  capabilities?: string[];
  resources?: JsonObject;
  status: string;
  health_status: string;
  current_task_id?: string | null;
  last_seen_at?: string;
}

export interface MachineRecord extends ApiRecord {
  hostname: string;
  trusted: boolean;
  labels?: JsonObject;
  resources?: JsonObject;
}

export interface TaskRecord extends ApiRecord {
  title: string;
  state: string;
  priority?: number;
  required_capabilities?: string[];
  metadata?: JsonObject;
  owner_agent_id?: string | null;
  leased_until?: string | null;
  attempt_count?: number;
  max_attempts?: number;
}

export interface TaskDetail {
  task: TaskRecord;
  history: ApiRecord[];
  evidence: ApiRecord[];
  reviews: ApiRecord[];
  publications: ApiRecord[];
  summary?: JsonObject;
}

export interface AgentItem {
  agent: AgentRecord;
  machine: MachineRecord | null;
  active_tasks: TaskRecord[];
  capacity: number;
  active_lease_count: number;
  availability: { eligible: boolean; reasons: string[] };
}

export interface DispatchCandidate {
  agent_id: string;
  agent_name: string;
  eligible: boolean;
  reasons: string[];
}

export interface DispatchTask {
  task: TaskRecord;
  tenant_id?: string | null;
  eligible_agent_count: number;
  candidates: DispatchCandidate[];
}

export interface RolloutStatus {
  rollout: ApiRecord;
  runtime: ApiRecord | null;
  events: ApiRecord[];
  latest_eval_run: ApiRecord | null;
}

export interface HermesStartup {
  ready?: boolean;
  warnings?: string[];
  operator_health?: {
    status?: string;
    state_refs_existing?: number;
    slack_activation_source?: string;
    secret_redaction_effective?: boolean;
    log_actionable_count?: number;
  };
  security?: JsonObject;
  slack?: JsonObject;
  logs?: JsonObject;
}

export interface ObservabilityEvent extends ApiRecord {
  sequence: number;
  kind: string;
  layer: string;
  source: string;
  level: string;
  name: string;
  subject_type?: string | null;
  subject_id?: string | null;
  value?: number | null;
  unit?: string;
  detail?: JsonObject;
  created_at: string;
}

export interface ObservabilitySummary {
  counts: Record<string, number>;
  levels: Record<string, number>;
  layers: Record<string, number>;
  latest: ObservabilityEvent[];
  latest_metrics: ObservabilityEvent[];
}

export interface CommandAuditRecord extends ApiRecord {
  command_id: string;
  agent_id: string;
  phase: string;
  argv: string[];
  cwd: string;
  task_id?: string | null;
  lease_id?: string | null;
  started_at?: string | null;
  completed_at?: string | null;
  duration_ms?: number | null;
  returncode?: number | null;
  stdout_sha256?: string | null;
  stderr_sha256?: string | null;
  stdout_bytes?: number | null;
  stderr_bytes?: number | null;
  metadata?: JsonObject;
  created_at: string;
}

export interface OperatorNotification extends ApiRecord {
  event_type: string;
  subject_type?: string | null;
  subject_id?: string | null;
  title: string;
  body: string;
  channels?: string[];
  metadata?: JsonObject;
  status: string;
  created_at: string;
  delivered_at?: string | null;
}

export interface DashboardData {
  overview: {
    counts: Record<string, number>;
    task_states: Record<string, number>;
    agent_statuses: Record<string, number>;
  };
  tenants: ApiRecord[];
  users: ApiRecord[];
  personas: ApiRecord[];
  hermes_instances: ApiRecord[];
  platform_bindings: ApiRecord[];
  machines: MachineRecord[];
  agents: AgentItem[];
  tasks: TaskDetail[];
  dead_letters: TaskRecord[];
  dispatch: { open_task_count: number; tasks: DispatchTask[] };
  messages: ApiRecord[];
  notifications: OperatorNotification[];
  command_audit: CommandAuditRecord[];
  secrets: ApiRecord[];
  secret_audits: ApiRecord[];
  runtimes: ApiRecord[];
  runtime_runs: ApiRecord[];
  rollouts: RolloutStatus[];
  eval_sets: ApiRecord[];
  eval_runs: ApiRecord[];
  observability: ObservabilitySummary;
  hermes_startup?: HermesStartup | null;
}

export interface DashboardState {
  activeView: ViewKey;
  token: string;
  loading: boolean;
  loadedAt: Date | null;
  data: DashboardData | null;
  error: string | null;
  actionMessage: string | null;
  agentQuery: string;
  agentFilter: string;
  taskFilter: string;
  observabilityLive: ObservabilityEvent[];
  observabilityStream: AbortController | null;
  observabilityStreamStatus: string;
}

export interface DashboardNodes {
  nav: HTMLElement;
  title: HTMLElement;
  banner: HTMLElement;
  content: HTMLElement;
  refresh: HTMLButtonElement;
  syncState: HTMLElement;
  tokenForm: HTMLFormElement;
  tokenInput: HTMLInputElement;
  clearToken: HTMLButtonElement;
}
