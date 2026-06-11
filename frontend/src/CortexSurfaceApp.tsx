import {
  Activity,
  BarChart3,
  Brain,
  CheckCircle2,
  CircleAlert,
  Cloud,
  FileText,
  FlaskConical,
  Hash,
  History,
  ImagePlus,
  ListChecks,
  Loader2,
  RefreshCcw,
  Sparkles,
  Target,
  Trash2,
  Trophy,
  Upload,
  Zap
} from "lucide-react";
import { FormEvent, useEffect, useId, useMemo, useRef, useState } from "react";
import * as THREE from "three";
import {
  analyzePost,
  createAbTest,
  createPost,
  deleteAbTest,
  deletePost,
  generatePostReport,
  getAbTest,
  getAbTests,
  getCalibration,
  getHealth,
  getPosts,
  mediaUrl
} from "./cortexApi";
import fsaverageMesh from "./assets/fsaverage5-pial.json";
import type { AbTest, Calibration, Health, LlmReport, NetworkScore, Post, Status, TemporalPoint } from "./types";

type Tab = "analyze" | "history" | "insights" | "ab";
type InsightTab = "hooks" | "top" | "patterns";

type HemisphereMesh = {
  coords: number[][];
  faces: number[][];
};

type BrainMeshAsset = {
  hemiVertexCount: number;
  left: HemisphereMesh;
  right: HemisphereMesh;
};

type SurfaceActivation = NonNullable<NonNullable<Post["analysis_summary"]>["surface"]>;

const anatomicalMesh = fsaverageMesh as BrainMeshAsset;

const ACTIVATION_LOW = new THREE.Color(0x00e5ff);
const ACTIVATION_MID = new THREE.Color(0xd8ff3d);
const ACTIVATION_HIGH = new THREE.Color(0xff2d55);
const ACTIVATION_OFF = new THREE.Color(0x000000);

const networkColors: Record<string, string> = {
  visual: "#4a9eff",
  attention: "#ff9f4a",
  language: "#a855f7",
  social: "#ff6b9d",
  memory_scene: "#3dd4c8",
  control: "#d8ff3d",
  motor: "#6bff9f",
  valuation: "#ff4a6b",
};

const tabs: Array<{ id: Tab; label: string; icon: typeof Brain }> = [
  { id: "analyze", label: "Analyze", icon: Brain },
  { id: "history", label: "Archive", icon: History },
  { id: "insights", label: "Insights", icon: Sparkles },
  { id: "ab", label: "A/B Testing", icon: FlaskConical }
];

const insightTabs: Array<{ id: InsightTab; label: string; icon: typeof Brain }> = [
  { id: "hooks", label: "Hooks", icon: Cloud },
  { id: "top", label: "Top 20", icon: Trophy },
  { id: "patterns", label: "Patterns", icon: Hash }
];

const networkLabels: Record<string, string> = {
  visual: "Visual",
  attention: "Attention",
  language: "Language",
  social: "Social",
  memory_scene: "Memory/Scene",
  control: "Executive control",
  motor: "Sensorimotor",
  valuation: "Valuation"
};

const networkMeanings: Record<string, string> = {
  visual: "visual form, color, object, and scene processing",
  attention: "attention allocation and salience tracking",
  language: "semantic and language-adjacent associations",
  social: "people, faces, and social-context processing",
  memory_scene: "scene, place, and memory-context processing",
  control: "executive-control and planning-associated response",
  motor: "body/action-associated sensorimotor response",
  valuation: "reward and value-associated response"
};

type InterpretationPoint = {
  label: string;
  value: string;
  body: string;
};

type Interpretation = {
  headline: string;
  summary: string;
  points: InterpretationPoint[];
  caveat: string;
};

type AbDecision = {
  winner: Post | null;
  basis: string;
  status: "waiting" | "chosen" | "failed";
  confidence: string;
  margin: string;
  body: string;
};

type WordTerm = {
  term: string;
  count: number;
  posts: number;
  weight: number;
};

type PhraseTerm = {
  phrase: string;
  count: number;
  posts: number;
  weight: number;
};

type TopHookRow = {
  post: Post;
  rank: number;
  hook: string;
};

type GroupStat = {
  label: string;
  count: number;
  avgLikes: number;
  maxLikes: number;
  hookCoverage: number;
};

type InsightAnalytics = {
  historicalCount: number;
  postsWithLikesCount: number;
  hooksAvailableCount: number;
  topLimit: number;
  topPosts: Post[];
  topPostsWithHooksCount: number;
  topWordCloud: WordTerm[];
  allWordCloud: WordTerm[];
  topHooks: TopHookRow[];
  phraseTerms: PhraseTerm[];
  postTypeStats: GroupStat[];
  entityStats: GroupStat[];
  avgTopLikes: number;
  medianTopLikes: number;
};

const TOP_POST_LIMIT = 20;
const WORD_CLOUD_LIMIT = 48;
const PHRASE_LIMIT = 14;

const KEEP_SHORT_WORDS = new Set(["ai", "gpt"]);
const STOP_WORDS = new Set([
  "a", "an", "and", "are", "as", "at", "be", "been", "being", "but", "by", "can", "did", "do", "does",
  "doing", "for", "from", "had", "has", "have", "her", "here", "him", "his", "how", "i", "if", "in",
  "inside", "into", "is", "it", "its", "just", "like", "more", "most", "not", "now", "of", "on", "one",
  "or", "our", "out", "over", "read", "she", "so", "than", "that", "the", "their", "them", "then",
  "there", "they", "this", "to", "too", "up", "use", "used", "using", "via", "was", "were", "what",
  "when", "where", "who", "why", "will", "with", "you", "your",
  "al", "del", "el", "en", "es", "la", "las", "los", "para", "por", "que", "se", "un", "una",
  "chatgptricks", "caption", "comment", "comments", "post", "posts", "read", "slide", "swipe", "thread"
]);

function CortexSurfaceApp() {
  const [activeTab, setActiveTab] = useState<Tab>("analyze");
  const [health, setHealth] = useState<Health | null>(null);
  const [calibration, setCalibration] = useState<Calibration | null>(null);
  const [singlePosts, setSinglePosts] = useState<Post[]>([]);
  const [historicalPosts, setHistoricalPosts] = useState<Post[]>([]);
  const [abTests, setAbTests] = useState<AbTest[]>([]);
  const [selectedTest, setSelectedTest] = useState<{ test: AbTest; candidates: Post[] } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const insights = useMemo(() => buildInsightAnalytics(historicalPosts), [historicalPosts]);

  async function refresh() {
    setError(null);
    const [healthResult, calibrationResult, singleResult, historicalResult, testsResult] = await Promise.all([
      getHealth(),
      getCalibration(),
      getPosts("single"),
      getPosts("historical"),
      getAbTests()
    ]);
    setHealth(healthResult);
    setCalibration(calibrationResult);
    setSinglePosts(singleResult.posts);
    setHistoricalPosts(historicalResult.posts);
    setAbTests(testsResult.tests);
    const selectedId = selectedTest?.test.id;
    const nextSelectedId = selectedId && testsResult.tests.some((test) => test.id === selectedId)
      ? selectedId
      : testsResult.tests[0]?.id;
    setSelectedTest(nextSelectedId ? await getAbTest(nextSelectedId) : null);
  }

  useEffect(() => {
    refresh().catch((caught) => setError(caught.message));
  }, []);

  const hasRunningJobs = useMemo(() => {
    const posts = [...singlePosts, ...historicalPosts, ...(selectedTest?.candidates ?? [])];
    return posts.some((post) => post.status === "queued" || post.status === "running");
  }, [historicalPosts, selectedTest, singlePosts]);

  useEffect(() => {
    if (!hasRunningJobs) return;
    const timer = window.setInterval(() => {
      refresh().catch((caught) => setError(caught.message));
    }, 5000);
    return () => window.clearInterval(timer);
  }, [hasRunningJobs, selectedTest?.test.id]);

  async function submitPost(event: FormEvent<HTMLFormElement>, section: "single" | "historical") {
    event.preventDefault();
    const formElement = event.currentTarget;
    setLoading(true);
    setError(null);
    try {
      const form = new FormData(formElement);
      form.set("section", section);
      await createPost(form);
      formElement.reset();
      await refresh();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not create the analysis.");
    } finally {
      setLoading(false);
    }
  }

  async function submitAbTest(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const formElement = event.currentTarget;
    setLoading(true);
    setError(null);
    try {
      const form = new FormData(formElement);
      const files = form.getAll("files") as File[];
      const titles = files.map((file, index) => form.get(`candidate_${index}`) || file.name);
      titles.forEach((_, index) => form.delete(`candidate_${index}`));
      form.set("candidate_titles", JSON.stringify(titles));
      const created = await createAbTest(form);
      formElement.reset();
      setSelectedTest(created);
      await refresh();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not create the A/B test.");
    } finally {
      setLoading(false);
    }
  }

  async function handleDeletePost(post: Post) {
    setError(null);
    try {
      await deletePost(post.id);
      await refresh();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not delete the result.");
    }
  }

  async function handleDeleteAbTest(test: AbTest) {
    setError(null);
    try {
      await deleteAbTest(test.id);
      await refresh();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not delete the A/B test.");
    }
  }

  return (
    <main className="app-shell">
      <header className="topbar">
        <div>
          <p className="product-name">Cortex by Sentient</p>
          <h1>Neural performance analysis for visual covers</h1>
        </div>
        <button className="icon-button" onClick={() => refresh().catch((caught) => setError(caught.message))}>
          <RefreshCcw size={18} />
          Refresh
        </button>
      </header>

      <HealthBanner health={health} />
      {error ? <div className="alert"><CircleAlert size={18} />{error}</div> : null}

      <nav className="tabs" aria-label="Sections">
        {tabs.map((tab) => {
          const Icon = tab.icon;
          return (
            <button
              key={tab.id}
              className={activeTab === tab.id ? "tab active" : "tab"}
              onClick={() => setActiveTab(tab.id)}
            >
              <Icon size={18} />
              {tab.label}
            </button>
          );
        })}
      </nav>

      {activeTab === "analyze" ? (
        <section className="workspace-grid">
          <UploadPanel
            title="New cover"
            description="Upload an image. The backend turns it into a short silent MP4 before sending it to TRIBE v2."
            onSubmit={(event) => submitPost(event, "single")}
            loading={loading}
          />
          <ResultsColumn
            posts={singlePosts}
            empty="No analyzed covers in this section yet."
            onAnalyze={async (post) => {
              await analyzePost(post.id);
              await refresh();
            }}
            onDelete={handleDeletePost}
          />
        </section>
      ) : null}

      {activeTab === "history" ? (
        <section className="workspace-grid wide-left">
          <div className="stack">
            <CalibrationPanel calibration={calibration} />
            <UploadPanel
              title="Historical post"
              description="Add published covers with real likes to calibrate the relationship between TRIBE v2 activation and performance."
              onSubmit={(event) => submitPost(event, "historical")}
              loading={loading}
              includeLikes
            />
          </div>
          <ResultsColumn
            posts={historicalPosts}
            empty="No historical posts yet. Add covers with real likes to train the calibration model."
            onAnalyze={async (post) => {
              await analyzePost(post.id);
              await refresh();
            }}
            onDelete={handleDeletePost}
          />
        </section>
      ) : null}

      {activeTab === "insights" ? (
        <InsightsPanel insights={insights} />
      ) : null}

      {activeTab === "ab" ? (
        <section className="workspace-grid">
          <div className="stack">
            <AbUploadPanel onSubmit={submitAbTest} loading={loading} />
            <TestList tests={abTests} selectedId={selectedTest?.test.id} onSelect={async (id) => {
              setSelectedTest(await getAbTest(id));
            }} onDelete={handleDeleteAbTest} />
          </div>
          <AbResults selectedTest={selectedTest} onDeleteCandidate={handleDeletePost} />
        </section>
      ) : null}
    </main>
  );
}

function InsightsPanel({ insights }: { insights: InsightAnalytics }) {
  const [activeInsightTab, setActiveInsightTab] = useState<InsightTab>("hooks");
  const scannedLabel = insights.historicalCount
    ? `${insights.historicalCount.toLocaleString()} historical posts scanned`
    : "No historical posts scanned";

  return (
    <section className="insights-shell">
      <div className="section-heading insights-heading">
        <div>
          <h2>Historical insights</h2>
          <p>{scannedLabel} · top {Math.min(insights.topLimit, insights.topPosts.length)} ranked by actual likes.</p>
        </div>
        <div className="insight-summary-pill">
          <Cloud size={15} />
          {insights.hooksAvailableCount.toLocaleString()} hooks
        </div>
      </div>

      <div className="insight-tabs" aria-label="Analysis views">
        {insightTabs.map((tab) => {
          const Icon = tab.icon;
          return (
            <button
              key={tab.id}
              className={activeInsightTab === tab.id ? "insight-tab active" : "insight-tab"}
              onClick={() => setActiveInsightTab(tab.id)}
            >
              <Icon size={16} />
              {tab.label}
            </button>
          );
        })}
      </div>

      {activeInsightTab === "hooks" ? <HookInsightView insights={insights} /> : null}
      {activeInsightTab === "top" ? <TopPostsInsightView insights={insights} /> : null}
      {activeInsightTab === "patterns" ? <PatternInsightView insights={insights} /> : null}
    </section>
  );
}

function HookInsightView({ insights }: { insights: InsightAnalytics }) {
  const coverage = insights.topPosts.length
    ? insights.topPostsWithHooksCount / insights.topPosts.length
    : 0;

  return (
    <div className="insight-grid">
      <section className="panel insight-metrics-panel">
        <div className="panel-title">
          <Target size={20} />
          <div>
            <h2>Top hook scan</h2>
            <p>OCR hooks from the best historical posts, ranked by actual likes.</p>
          </div>
        </div>
        <div className="metric-grid">
          <Metric label="Top posts" value={String(insights.topPosts.length)} />
          <Metric label="With hooks" value={`${insights.topPostsWithHooksCount}/${insights.topPosts.length}`} />
          <Metric label="Coverage" value={formatPercent(coverage)} />
          <Metric label="Median likes" value={formatCompactNumber(insights.medianTopLikes)} />
        </div>
      </section>

      <section className="panel word-cloud-panel">
        <div className="panel-title">
          <Cloud size={20} />
          <div>
            <h2>Word cloud</h2>
            <p>Most repeated non-filler words in hooks from the top 20 posts.</p>
          </div>
        </div>
        <WordCloud terms={insights.topWordCloud} empty="No hook words found in the current top 20." />
      </section>

      <section className="panel hook-list-panel">
        <div className="panel-title">
          <ListChecks size={20} />
          <div>
            <h2>Winning hooks</h2>
            <p>The highest-liked posts that have OCR hook text available.</p>
          </div>
        </div>
        <div className="hook-list">
          {insights.topHooks.map((row) => (
            <div className="hook-row" key={row.post.id}>
              <div className="hook-rank">#{row.rank}</div>
              <div>
                <strong>{row.post.title}</strong>
                <p>{row.hook}</p>
              </div>
              <span>{formatCompactNumber(row.post.likes ?? 0)}</span>
            </div>
          ))}
          {insights.topHooks.length === 0 ? <div className="empty-state flat">No top hooks available yet.</div> : null}
        </div>
      </section>
    </div>
  );
}

function TopPostsInsightView({ insights }: { insights: InsightAnalytics }) {
  return (
    <div className="insight-grid top-posts-grid">
      <section className="panel insight-metrics-panel">
        <div className="panel-title">
          <Trophy size={20} />
          <div>
            <h2>Top 20 posts</h2>
            <p>Best historical posts by real likes, with hook coverage and source metadata.</p>
          </div>
        </div>
        <div className="metric-grid">
          <Metric label="Avg likes" value={formatCompactNumber(insights.avgTopLikes)} />
          <Metric label="Median likes" value={formatCompactNumber(insights.medianTopLikes)} />
          <Metric label="Best post" value={formatCompactNumber(insights.topPosts[0]?.likes ?? 0)} />
          <Metric label="Hooks" value={`${insights.topPostsWithHooksCount}/${insights.topPosts.length}`} />
        </div>
      </section>

      <section className="panel top-posts-panel">
        <div className="leaderboard-list">
          {insights.topPosts.map((post, index) => (
            <div className="leaderboard-row" key={post.id}>
              <div className="leaderboard-rank">{index + 1}</div>
              <div className="leaderboard-cover">
                {post.image_url ? <img src={mediaUrl(post.image_url)} alt={post.title} /> : null}
              </div>
              <div className="leaderboard-copy">
                <strong>{post.title}</strong>
                <p>{compactText(post.hook_text || post.caption || "No hook text available.", 150)}</p>
              </div>
              <div className="leaderboard-meta">
                <strong>{(post.likes ?? 0).toLocaleString()}</strong>
                <span>{post.post_type_label || "Unlabeled"}</span>
              </div>
            </div>
          ))}
          {insights.topPosts.length === 0 ? <div className="empty-state flat">No liked historical posts available.</div> : null}
        </div>
      </section>
    </div>
  );
}

function PatternInsightView({ insights }: { insights: InsightAnalytics }) {
  return (
    <div className="insight-grid pattern-grid">
      <section className="panel word-cloud-panel">
        <div className="panel-title">
          <Cloud size={20} />
          <div>
            <h2>All hooks cloud</h2>
            <p>Word frequency across every historical post with hook OCR.</p>
          </div>
        </div>
        <WordCloud terms={insights.allWordCloud} empty="No hooks have been extracted yet." />
      </section>

      <section className="panel phrase-panel">
        <div className="panel-title">
          <Hash size={20} />
          <div>
            <h2>Repeated phrases</h2>
            <p>Two-word patterns that recur inside top hooks.</p>
          </div>
        </div>
        <div className="phrase-list">
          {insights.phraseTerms.map((item) => (
            <span key={item.phrase} style={{ opacity: 0.72 + item.weight * 0.28 }}>
              {item.phrase}
              <small>{item.count}</small>
            </span>
          ))}
          {insights.phraseTerms.length === 0 ? <div className="empty-state flat">No repeated top-hook phrases yet.</div> : null}
        </div>
      </section>

      <section className="panel stat-list-panel">
        <div className="panel-title">
          <BarChart3 size={20} />
          <div>
            <h2>Post type lift</h2>
            <p>Types ranked by average likes when at least two posts exist.</p>
          </div>
        </div>
        <GroupStatList stats={insights.postTypeStats} empty="No post type patterns available yet." />
      </section>

      <section className="panel stat-list-panel">
        <div className="panel-title">
          <Sparkles size={20} />
          <div>
            <h2>Entity lift</h2>
            <p>People and company labels ranked by average likes.</p>
          </div>
        </div>
        <GroupStatList stats={insights.entityStats} empty="No entity patterns available yet." />
      </section>
    </div>
  );
}

function WordCloud({ terms, empty }: { terms: WordTerm[]; empty: string }) {
  if (!terms.length) return <div className="empty-state flat">{empty}</div>;
  const colors = ["#d8ff3d", "#00e5ff", "#ff6b9d", "#ff9f4a", "#b7ff51", "#a855f7"];
  return (
    <div className="word-cloud" aria-label="Word cloud">
      {terms.map((term, index) => (
        <span
          key={term.term}
          className="cloud-word"
          style={{
            color: colors[index % colors.length],
            fontSize: `${0.86 + term.weight * 1.85}rem`,
            opacity: 0.72 + term.weight * 0.28
          }}
          title={`${term.count} mentions across ${term.posts} posts`}
        >
          {term.term}
        </span>
      ))}
    </div>
  );
}

function GroupStatList({ stats, empty }: { stats: GroupStat[]; empty: string }) {
  if (!stats.length) return <div className="empty-state flat">{empty}</div>;
  const maxAvg = Math.max(...stats.map((stat) => stat.avgLikes), 1);
  return (
    <div className="group-stat-list">
      {stats.map((stat) => (
        <div className="group-stat-row" key={stat.label}>
          <div>
            <strong>{stat.label}</strong>
            <span>{stat.count} posts · {formatPercent(stat.hookCoverage)} hooks</span>
          </div>
          <div className="group-stat-bar">
            <i style={{ width: `${Math.max(4, (stat.avgLikes / maxAvg) * 100)}%` }} />
          </div>
          <span>{formatCompactNumber(stat.avgLikes)}</span>
        </div>
      ))}
    </div>
  );
}

function HealthBanner({ health }: { health: Health | null }) {
  if (!health) return <div className="status-strip muted">Checking backend connection...</div>;
  const tribe = health.tribev2;
  if (!tribe.installed) {
    return (
      <div className="status-strip warning">
        <CircleAlert size={18} />
        TRIBE v2 is not installed in the backend. The app cannot generate results until the real dependencies are installed.
      </div>
    );
  }
  return (
    <div className="status-strip">
      <CheckCircle2 size={18} />
      <span>TRIBE v2 installed</span>
      <span>Model: {tribe.model_id}</span>
      <span>Device: {tribe.device}</span>
      <span>{tribe.hf_token_present ? "HF token detected" : "HF token missing"}</span>
      <span>{health.remote_tribe?.configured ? "Remote GPU enabled" : "Local TRIBE mode"}</span>
      <span>Report LLM: {health.llm_report?.model_id ?? "Not configured"}</span>
    </div>
  );
}

function UploadPanel({
  title,
  description,
  onSubmit,
  loading,
  includeLikes = false
}: {
  title: string;
  description: string;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
  loading: boolean;
  includeLikes?: boolean;
}) {
  return (
    <form className="panel upload-panel" onSubmit={onSubmit}>
      <div className="panel-title">
        <ImagePlus size={20} />
        <div>
          <h2>{title}</h2>
          <p>{description}</p>
        </div>
      </div>
      <label>
        Title
        <input name="title" placeholder="Friday release cover" required />
      </label>
      <FileInput label="Image" name="file" />
      <div className="form-row">
        <label>
          Video duration
          <input name="duration_seconds" type="number" min="2" max="10" defaultValue="2" />
        </label>
        {includeLikes ? (
          <label>
            Actual likes
            <input name="likes" type="number" min="0" required />
          </label>
        ) : null}
      </div>
      <label>
        Publish date
        <input name="published_at" type="date" />
      </label>
      <label>
        Notes
        <textarea name="caption" rows={3} placeholder="Post context, song, design direction, or hypothesis" />
      </label>
      <button className="primary-button" disabled={loading}>
        {loading ? <Loader2 className="spin" size={18} /> : <Upload size={18} />}
        Analyze with TRIBE v2
      </button>
    </form>
  );
}

function ResultsColumn({
  posts,
  empty,
  onAnalyze,
  onDelete
}: {
  posts: Post[];
  empty: string;
  onAnalyze?: (post: Post) => void;
  onDelete: (post: Post) => Promise<void>;
}) {
  return (
    <div className="stack">
      {posts.length === 0 ? <div className="empty-state">{empty}</div> : null}
      {posts.map((post) => <PostResult key={post.id} post={post} onAnalyze={onAnalyze} onDelete={onDelete} />)}
    </div>
  );
}

function PostResult({
  post,
  onAnalyze,
  onDelete
}: {
  post: Post;
  onAnalyze?: (post: Post) => void;
  onDelete: (post: Post) => Promise<void>;
}) {
  return (
    <article className="result-card">
      <div className="cover-frame">
        {post.image_url ? <img src={mediaUrl(post.image_url)} alt={post.title} /> : null}
      </div>
      <div className="result-body">
        <div className="result-header">
          <div>
            <h3>{post.title}</h3>
            <p>{post.published_at || "No date"}</p>
          </div>
          <div className="result-actions">
            <StatusPill status={post.status} />
            <DeleteAction
              ariaLabel={`Delete ${post.title}`}
              confirmText="Delete this result and its files?"
              onConfirm={() => onDelete(post)}
            />
          </div>
        </div>
        <ProgressIndicator post={post} />
        {post.error ? <div className="inline-error">{post.error}</div> : null}
        {onAnalyze && (post.status === "failed" || post.status === "completed") ? (
          <button className="text-button reanalyze-button" onClick={() => onAnalyze(post)}>
            <Activity size={16} />
            Reanalyze
          </button>
        ) : null}
        {post.analysis_summary ? <BrainSummary post={post} /> : <PendingCopy status={post.status} />}
      </div>
    </article>
  );
}

function BrainSummary({ post }: { post: Post }) {
  const summary = post.analysis_summary;
  if (!summary) return null;
  const metrics = summary.metrics;
  const prediction = post.calibrated_prediction;
  const isHistorical = post.section === "historical";

  return (
    <div className="brain-summary">
      <ViralityGauge score={summary.virality_potential ?? computeViralityPotential(summary)} />

      <div className="metric-grid">
        <Metric label="Mean activation" value={formatMetric(metrics.global_mean_abs)} />
        <Metric label="Global peak" value={formatMetric(metrics.global_peak_abs)} />
        <Metric label="Archive percentile" value={post.tribe_percentile ? `P${post.tribe_percentile}` : "N/A"} />
        <Metric label="Segments" value={String(metrics.n_segments ?? 0)} />
      </div>

      {prediction ? (
        <div className="prediction-row">
          <div className="prediction-box">
            <Trophy size={18} />
            <span>{prediction.predicted_likes.toLocaleString()} predicted likes</span>
            <small>{Math.round(prediction.confidence * 100)}% conf.</small>
          </div>
          {isHistorical && post.likes != null ? (
            <div className="actual-box">
              <BarChart3 size={16} />
              <span>{post.likes.toLocaleString()} actual</span>
              <small>
                {prediction.predicted_likes > post.likes ? "+" : ""}
                {Math.round((prediction.predicted_likes - post.likes) / Math.max(post.likes, 1) * 100)}% error
              </small>
            </div>
          ) : null}
        </div>
      ) : isHistorical && post.likes != null ? (
        <div className="prediction-box actual-only">
          <BarChart3 size={18} />
          <span>{post.likes.toLocaleString()} actual likes</span>
          <small>Calibration training</small>
        </div>
      ) : (
        <div className="prediction-box muted">
          <BarChart3 size={18} />
          <span>Add historical posts with likes to predict performance</span>
        </div>
      )}

      <div className="brain-and-networks">
        <BrainActivationView summary={summary} />
        <NetworkBars networks={summary.networks} />
      </div>
      {summary.temporal_series.length > 2 ? <TemporalChart points={summary.temporal_series} /> : null}
      <InterpretationPanel post={post} />
      <LlmReportPanel post={post} />
      {summary.warnings.length ? <div className="inline-warning">{summary.warnings[0]}</div> : null}
    </div>
  );
}

function BrainActivationView({ summary }: { summary: NonNullable<Post["analysis_summary"]> }) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    const surface = summary.surface;
    if (!canvas || !surface || !surface.values.length) return;

    const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    renderer.setClearColor(0x000000, 0);
    renderer.shadowMap.enabled = false;

    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(36, 1, 0.1, 100);
    camera.position.set(0, 0.3, 7.5);

    const ambient = new THREE.AmbientLight(0x8ca89a, 0.9);
    const key = new THREE.DirectionalLight(0xffffff, 3.2);
    key.position.set(3, 5, 3);
    const rim = new THREE.DirectionalLight(0xd8ff3d, 2.6);
    rim.position.set(-3, 1, -3);
    const fill = new THREE.DirectionalLight(0x2244aa, 1.1);
    fill.position.set(0, -3, 1.5);
    scene.add(ambient, key, rim, fill);

    const group = new THREE.Group();
    scene.add(group);

    const shellMaterial = new THREE.MeshStandardMaterial({
      color: 0x8faaa0,
      roughness: 0.82,
      metalness: 0.06,
      transparent: true,
      opacity: 0.18,
      side: THREE.DoubleSide
    });

    const activationMaterial = new THREE.MeshBasicMaterial({
      vertexColors: true,
      transparent: true,
      opacity: 0.98,
      blending: THREE.AdditiveBlending,
      depthWrite: false,
      depthTest: true,
      side: THREE.FrontSide
    });

    const leftShell = makeHemisphereShell(anatomicalMesh.left, shellMaterial);
    const rightShell = makeHemisphereShell(anatomicalMesh.right, shellMaterial);
    const leftActivation = makeActivationSurface(anatomicalMesh.left, 0, surface, activationMaterial);
    const rightActivation = makeActivationSurface(
      anatomicalMesh.right,
      anatomicalMesh.hemiVertexCount,
      surface,
      activationMaterial
    );
    group.add(leftShell, rightShell, leftActivation, rightActivation);

    const resize = () => {
      const rect = canvas.getBoundingClientRect();
      renderer.setSize(rect.width, rect.height, false);
      camera.aspect = rect.width / Math.max(1, rect.height);
      camera.updateProjectionMatrix();
    };
    resize();
    window.addEventListener("resize", resize);

    let pointerDown = false;
    let previousX = 0;
    const onPointerDown = (event: PointerEvent) => {
      pointerDown = true;
      previousX = event.clientX;
    };
    const onPointerMove = (event: PointerEvent) => {
      if (!pointerDown) return;
      const dx = event.clientX - previousX;
      previousX = event.clientX;
      group.rotation.y += dx * 0.01;
    };
    const onPointerUp = () => {
      pointerDown = false;
    };
    canvas.addEventListener("pointerdown", onPointerDown);
    window.addEventListener("pointermove", onPointerMove);
    window.addEventListener("pointerup", onPointerUp);

    group.rotation.y = -Math.PI * 0.42;

    let frame = 0;
    const animate = () => {
      frame = window.requestAnimationFrame(animate);
      if (!pointerDown) group.rotation.y += 0.0018;
      group.rotation.x = -0.14;
      renderer.render(scene, camera);
    };
    animate();

    return () => {
      window.cancelAnimationFrame(frame);
      window.removeEventListener("resize", resize);
      canvas.removeEventListener("pointerdown", onPointerDown);
      window.removeEventListener("pointermove", onPointerMove);
      window.removeEventListener("pointerup", onPointerUp);
      renderer.dispose();
      leftShell.geometry.dispose();
      rightShell.geometry.dispose();
      leftActivation.geometry.dispose();
      rightActivation.geometry.dispose();
      shellMaterial.dispose();
      activationMaterial.dispose();
    };
  }, [summary]);

  if (!summary.surface?.values.length) return null;
  return (
    <div className="brain-viewport">
      <canvas ref={canvasRef} aria-label="3D brain with TRIBE v2 estimated activation" />
      <div className="brain-legend">
        <span>Low</span>
        <i />
        <span>High</span>
      </div>
    </div>
  );
}

function InterpretationPanel({ post }: { post: Post }) {
  const interpretation = interpretPost(post);
  if (!interpretation) return null;
  return (
    <section className="interpretation-panel" aria-label="Result interpretation">
      <div className="interpretation-title">
        <Brain size={16} />
        <div>
          <h4>{interpretation.headline}</h4>
          <p>{interpretation.summary}</p>
        </div>
      </div>
      <div className="interpretation-grid">
        {interpretation.points.map((point) => (
          <div className="interpretation-item" key={point.label}>
            <span>{point.label}</span>
            <strong>{point.value}</strong>
            <p>{point.body}</p>
          </div>
        ))}
      </div>
      <p className="interpretation-caveat">{interpretation.caveat}</p>
    </section>
  );
}

function LlmReportPanel({ post }: { post: Post }) {
  const [report, setReport] = useState<LlmReport | null>(post.llm_report ?? null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setReport(post.llm_report ?? null);
    setError(null);
  }, [post.id, post.llm_report]);

  async function requestReport(force = false) {
    setLoading(true);
    setError(null);
    try {
      const result = await generatePostReport(post.id, force);
      setReport(result.report);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not generate the LLM report.");
    } finally {
      setLoading(false);
    }
  }

  return (
    <section className="llm-report-panel" aria-label="LLM report">
      <div className="llm-report-header">
        <div>
          <h4><FileText size={16} />AI report</h4>
          <p>
            {report
              ? `${report.model} via ${report.provider} · ${new Date(report.generated_at).toLocaleString()}`
              : "Generate a LLaMA-written readout from this TRIBE v2 result."}
          </p>
        </div>
        <button className="text-button compact-action" disabled={loading} onClick={() => requestReport(Boolean(report))}>
          {loading ? <Loader2 className="spin" size={15} /> : <FileText size={15} />}
          {report ? "Regenerate" : "Generate"}
        </button>
      </div>
      {error ? <div className="inline-error">{error}</div> : null}
      {report ? <div className="llm-report-body">{report.report}</div> : null}
    </section>
  );
}

function makeHemisphereShell(hemisphere: HemisphereMesh, material: THREE.Material) {
  const geometry = meshGeometry(hemisphere);
  const mesh = new THREE.Mesh(geometry, material);
  return mesh;
}

function meshGeometry(hemisphere: HemisphereMesh) {
  const positions = new Float32Array(hemisphere.coords.length * 3);
  hemisphere.coords.forEach((coord, index) => {
    const point = transformFsAverageCoord(coord);
    positions[index * 3] = point.x;
    positions[index * 3 + 1] = point.y;
    positions[index * 3 + 2] = point.z;
  });
  const indices = new Uint32Array(hemisphere.faces.length * 3);
  hemisphere.faces.forEach((face, index) => {
    indices[index * 3] = face[0];
    indices[index * 3 + 1] = face[1];
    indices[index * 3 + 2] = face[2];
  });
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
  geometry.setIndex(new THREE.BufferAttribute(indices, 1));
  geometry.computeVertexNormals();
  return geometry;
}

function makeActivationSurface(
  hemisphere: HemisphereMesh,
  vertexOffset: number,
  surface: SurfaceActivation,
  material: THREE.Material
) {
  const geometry = meshGeometry(hemisphere);
  const activations = activationValuesForHemisphere(hemisphere, vertexOffset, surface);
  const colors = new Float32Array(activations.length * 3);
  const color = new THREE.Color();

  activations.forEach((value, i) => {
    activationColor(value, color);
    colors[i * 3] = color.r;
    colors[i * 3 + 1] = color.g;
    colors[i * 3 + 2] = color.b;
  });

  geometry.setAttribute("color", new THREE.BufferAttribute(colors, 3));
  const mesh = new THREE.Mesh(geometry, material);
  mesh.renderOrder = 3;
  return mesh;
}

type ActivationSample = {
  localIndex: number;
  value: number;
  coord: number[];
};

function activationValuesForHemisphere(
  hemisphere: HemisphereMesh,
  vertexOffset: number,
  surface: SurfaceActivation
) {
  const count = hemisphere.coords.length;
  const directValues = new Array<number>(count).fill(Number.NaN);
  const samples: ActivationSample[] = [];

  surface.values.forEach((value, samplePosition) => {
    const globalIndex = surface.sample_indices[samplePosition] ?? samplePosition;
    const localIndex = globalIndex - vertexOffset;
    if (localIndex < 0 || localIndex >= count) return;
    const activation = clamp01(value);
    directValues[localIndex] = activation;
    samples.push({
      localIndex,
      value: activation,
      coord: hemisphere.coords[localIndex]
    });
  });

  if (!samples.length) {
    return new Array<number>(count).fill(0);
  }

  const complete = samples.length >= count && directValues.every((value) => Number.isFinite(value));
  if (complete) {
    return directValues;
  }

  return hemisphere.coords.map((coord, index) => {
    const direct = directValues[index];
    if (Number.isFinite(direct)) return direct;
    return spatiallyInterpolatedActivation(coord, samples);
  });
}

function spatiallyInterpolatedActivation(coord: number[], samples: ActivationSample[]) {
  let d1 = Number.POSITIVE_INFINITY;
  let d2 = Number.POSITIVE_INFINITY;
  let d3 = Number.POSITIVE_INFINITY;
  let d4 = Number.POSITIVE_INFINITY;
  let v1 = 0;
  let v2 = 0;
  let v3 = 0;
  let v4 = 0;

  for (const sample of samples) {
    const dx = coord[0] - sample.coord[0];
    const dy = coord[1] - sample.coord[1];
    const dz = coord[2] - sample.coord[2];
    const distance = dx * dx + dy * dy + dz * dz;
    if (distance <= 1e-10) return sample.value;

    if (distance < d1) {
      d4 = d3; v4 = v3;
      d3 = d2; v3 = v2;
      d2 = d1; v2 = v1;
      d1 = distance; v1 = sample.value;
    } else if (distance < d2) {
      d4 = d3; v4 = v3;
      d3 = d2; v3 = v2;
      d2 = distance; v2 = sample.value;
    } else if (distance < d3) {
      d4 = d3; v4 = v3;
      d3 = distance; v3 = sample.value;
    } else if (distance < d4) {
      d4 = distance; v4 = sample.value;
    }
  }

  const w1 = interpolationWeight(d1);
  const w2 = interpolationWeight(d2);
  const w3 = interpolationWeight(d3);
  const w4 = interpolationWeight(d4);
  const total = w1 + w2 + w3 + w4;
  return total > 0 ? (v1 * w1 + v2 * w2 + v3 * w3 + v4 * w4) / total : 0;
}

function interpolationWeight(distance: number) {
  if (!Number.isFinite(distance)) return 0;
  return 1 / Math.pow(Math.sqrt(distance) + 0.035, 2);
}

function activationColor(value: number, color: THREE.Color) {
  const base = clamp01(value);
  if (base < 0.022) {
    color.copy(ACTIVATION_OFF);
    return;
  }

  const shaped = Math.pow((base - 0.022) / 0.978, 0.44);
  if (shaped < 0.55) {
    color.copy(ACTIVATION_LOW).lerp(ACTIVATION_MID, shaped / 0.55);
  } else {
    color.copy(ACTIVATION_MID).lerp(ACTIVATION_HIGH, (shaped - 0.55) / 0.45);
  }
}

function clamp01(value: number) {
  if (!Number.isFinite(value)) return 0;
  return Math.max(0, Math.min(1, value));
}

function transformFsAverageCoord(coord: number[], expand = 1) {
  const scale = 2.4 * expand;
  return {
    x: coord[0] * scale,
    y: coord[2] * scale,
    z: -coord[1] * scale
  };
}

function NetworkBars({ networks }: { networks: Record<string, NetworkScore> }) {
  const sorted = Object.entries(networks).sort(([, a], [, b]) => b.score - a.score);
  return (
    <div className="network-grid">
      {sorted.map(([key, network]) => {
        const color = networkColors[key] ?? "var(--accent)";
        return (
          <div className="network-item" key={key}>
            <span style={{ color }}>{networkLabels[key] ?? network.label}</span>
            <div>
              <i style={{
                width: `${Math.max(2, network.score)}%`,
                background: color,
                boxShadow: network.score > 60 ? `0 0 6px ${color}66` : undefined
              }} />
            </div>
            <strong>{Math.round(network.score)}</strong>
          </div>
        );
      })}
    </div>
  );
}

function ViralityGauge({ score }: { score: number }) {
  const pct = Math.round(score * 100);
  const color = score >= 0.70 ? "#d8ff3d"
    : score >= 0.50 ? "#a6ff00"
    : score >= 0.30 ? "#1cff93"
    : "#4a8c6a";
  const label = score >= 0.75 ? "High potential"
    : score >= 0.50 ? "Moderate potential"
    : score >= 0.30 ? "Low potential"
    : "Minimal signal";

  return (
    <div className="virality-gauge">
      <div className="gauge-score" style={{ color }}>
        {pct}<span>%</span>
      </div>
      <div className="gauge-body">
        <div className="gauge-labels">
          <strong style={{ color }}>{label}</strong>
          <small>Viral potential · Social · Reward · Attention</small>
        </div>
        <div className="gauge-track">
          <div
            className="gauge-fill"
            style={{
              width: `${pct}%`,
              background: `linear-gradient(90deg, #1cff93, ${color})`,
              boxShadow: score > 0.45 ? `0 0 10px ${color}99` : undefined,
            }}
          />
        </div>
      </div>
    </div>
  );
}

function TemporalChart({ points }: { points: TemporalPoint[] }) {
  if (!points.length) return null;
  const W = 520;
  const H = 128;
  const padT = 12, padB = 24, padL = 8, padR = 8;
  const innerW = W - padL - padR;
  const innerH = H - padT - padB;
  const max = Math.max(...points.map((p) => p.mean_abs), 1e-9);
  const step = innerW / Math.max(points.length - 1, 1);

  const pts = points.map((p, i) => ({
    x: padL + i * step,
    y: padT + innerH - (p.mean_abs / max) * innerH,
  }));

  const area = [
    `M ${pts[0].x} ${H - padB}`,
    ...pts.map((p) => `L ${p.x} ${p.y}`),
    `L ${pts[pts.length - 1].x} ${H - padB}`,
    "Z",
  ].join(" ");

  const line = pts.map((p, i) => `${i === 0 ? "M" : "L"} ${p.x.toFixed(1)} ${p.y.toFixed(1)}`).join(" ");

  const peakIdx = points.reduce((best, p, i) => p.mean_abs > points[best].mean_abs ? i : best, 0);
  const peakPt = pts[peakIdx];

  return (
    <div className="temporal-wrap">
      <p className="temporal-label">Temporal activation</p>
      <svg className="temporal-chart" viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Temporal activation">
        <defs>
          <linearGradient id="tg" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="var(--accent)" stopOpacity="0.18" />
            <stop offset="100%" stopColor="var(--accent)" stopOpacity="0" />
          </linearGradient>
        </defs>
        <path d={area} fill="url(#tg)" />
        <path d={line} fill="none" stroke="var(--accent)" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" />
        <circle cx={peakPt.x} cy={peakPt.y} r="5" fill="var(--accent)" />
        <text x={peakPt.x} y={peakPt.y - 9} textAnchor="middle" fill="var(--accent)" fontSize="11" fontWeight="800">peak</text>
        <text x={padL} y={H - 6} fill="#4a5e45" fontSize="10">0s</text>
        {points.length > 1 ? (
          <text x={padL + innerW} y={H - 6} textAnchor="end" fill="#4a5e45" fontSize="10">
            {(points[points.length - 1].start + points[points.length - 1].duration).toFixed(0)}s
          </text>
        ) : null}
      </svg>
    </div>
  );
}

function CalibrationPanel({ calibration }: { calibration: Calibration | null }) {
  return (
    <section className="panel calibration-panel">
      <div className="panel-title">
        <Zap size={20} />
        <div>
          <h2>Local calibration</h2>
          <p>{calibration?.message || "Loading calibration..."}</p>
        </div>
      </div>
      <div className="metric-grid">
        <Metric label="Samples" value={String(calibration?.sample_count ?? 0)} />
        <Metric label="Status" value={calibration?.ready ? "Ready" : "Pending"} />
        <Metric
          label="Training R2"
          value={calibration?.r2_training == null ? "N/A" : calibration.r2_training.toFixed(2)}
        />
      </div>
    </section>
  );
}


function AbUploadPanel({ onSubmit, loading }: { onSubmit: (event: FormEvent<HTMLFormElement>) => void; loading: boolean }) {
  return (
    <form className="panel upload-panel" onSubmit={onSubmit}>
      <div className="panel-title">
        <FlaskConical size={20} />
        <div>
          <h2>Compare covers</h2>
          <p>Upload two or more candidates. Rankings use calibrated likes when enough historical data exists.</p>
        </div>
      </div>
      <label>
        Test name
        <input name="name" placeholder="Acoustic release" required />
      </label>
      <FileInput label="Candidates" name="files" multiple />
      <label>
        Video duration
        <input name="duration_seconds" type="number" min="2" max="10" defaultValue="2" />
      </label>
      <button className="primary-button" disabled={loading}>
        {loading ? <Loader2 className="spin" size={18} /> : <Upload size={18} />}
        Start comparison
      </button>
    </form>
  );
}

function TestList({
  tests,
  selectedId,
  onSelect,
  onDelete
}: {
  tests: AbTest[];
  selectedId?: number;
  onSelect: (id: number) => void;
  onDelete: (test: AbTest) => Promise<void>;
}) {
  return (
    <section className="panel compact-panel">
      <h2>Saved tests</h2>
      <div className="test-list">
        {tests.map((test) => (
          <div key={test.id} className={selectedId === test.id ? "test-item active" : "test-item"}>
            <button className="test-select" onClick={() => onSelect(test.id)}>
              <span>{test.name}</span>
              <small>{test.status === "completed" ? "Winner chosen" : test.status === "failed" ? "No winner" : "Running"}</small>
            </button>
            <DeleteAction
              ariaLabel={`Delete ${test.name}`}
              confirmText="Delete this test and its candidate results?"
              onConfirm={() => onDelete(test)}
            />
          </div>
        ))}
        {tests.length === 0 ? <div className="empty-state">No A/B tests yet.</div> : null}
      </div>
    </section>
  );
}

function AbResults({
  selectedTest,
  onDeleteCandidate
}: {
  selectedTest: { test: AbTest; candidates: Post[] } | null;
  onDeleteCandidate: (post: Post) => Promise<void>;
}) {
  if (!selectedTest) return <div className="empty-state">Create or select an A/B test.</div>;
  const decision = abDecision(selectedTest.test, selectedTest.candidates);
  return (
    <section className="stack">
      <div className="section-heading">
        <h2>{selectedTest.test.name}</h2>
        <p>{decision.status === "chosen" ? "The app has selected a winner for this comparison." : "Comparative ranking for potential covers."}</p>
      </div>
      <AbDecisionBanner decision={decision} />
      {selectedTest.candidates.map((candidate) => (
        <article key={candidate.id} className={candidate.is_winner ? "result-card winner" : "result-card"}>
          <div className={candidate.is_winner ? "rank-badge winner-badge" : "rank-badge"}>
            {candidate.is_winner ? <Trophy size={17} /> : candidate.rank ?? "-"}
          </div>
          <div className="cover-frame">
            {candidate.image_url ? <img src={mediaUrl(candidate.image_url)} alt={candidate.title} /> : null}
          </div>
          <div className="result-body">
            <div className="result-header">
              <div>
                <h3>{candidate.title}</h3>
                <p>{candidate.ranking_basis === "calibrated_likes" ? "Ranked by calibrated likes" : "Ranked by TRIBE v2 activation"}</p>
              </div>
              <div className="result-actions">
                {candidate.is_winner ? <span className="winner-pill"><Trophy size={14} />Chosen winner</span> : null}
                <StatusPill status={candidate.status} />
                <DeleteAction
                  ariaLabel={`Delete ${candidate.title}`}
                  confirmText="Delete this candidate result?"
                  onConfirm={() => onDeleteCandidate(candidate)}
                />
              </div>
            </div>
            <ProgressIndicator post={candidate} />
            {candidate.analysis_summary ? <BrainSummary post={candidate} /> : <PendingCopy status={candidate.status} />}
          </div>
        </article>
      ))}
    </section>
  );
}

function AbDecisionBanner({ decision }: { decision: AbDecision }) {
  if (decision.status === "waiting") {
    return (
      <section className="ab-decision pending-copy">
        <Loader2 className="spin" size={18} />
        Waiting for every candidate to finish before choosing a winner.
      </section>
    );
  }

  if (decision.status === "failed" || !decision.winner) {
    return (
      <section className="ab-decision inline-warning">
        <CircleAlert size={18} />
        No winner could be selected because no candidate completed successfully.
      </section>
    );
  }

  return (
    <section className="ab-decision winner-decision">
      <div className="decision-title">
        <Trophy size={19} />
        <div>
          <span>Chosen winner</span>
          <strong>{decision.winner.title}</strong>
        </div>
      </div>
      <div className="decision-grid">
        <Metric label="Decision basis" value={decision.basis} />
        <Metric label="Confidence" value={decision.confidence} />
        <Metric label="Margin" value={decision.margin} />
      </div>
      <p>{decision.body}</p>
    </section>
  );
}

function StatusPill({ status }: { status: Status }) {
  const labels: Record<Status, string> = {
    queued: "Queued",
    running: "Analyzing",
    completed: "Complete",
    failed: "Error"
  };
  return <span className={`status-pill ${status}`}>{labels[status]}</span>;
}

function DeleteAction({
  ariaLabel,
  confirmText,
  onConfirm
}: {
  ariaLabel: string;
  confirmText: string;
  onConfirm: () => Promise<void>;
}) {
  const [confirming, setConfirming] = useState(false);
  const [deleting, setDeleting] = useState(false);

  if (confirming) {
    return (
      <div className="delete-confirm">
        <span>{confirmText}</span>
        <button className="text-button compact-action" disabled={deleting} onClick={() => setConfirming(false)}>
          Cancel
        </button>
        <button
          className="text-button danger-button compact-action"
          disabled={deleting}
          onClick={async () => {
            setDeleting(true);
            await onConfirm();
            setDeleting(false);
            setConfirming(false);
          }}
        >
          {deleting ? <Loader2 className="spin" size={15} /> : <Trash2 size={15} />}
          Delete
        </button>
      </div>
    );
  }

  return (
    <button className="text-button danger-button delete-trigger" aria-label={ariaLabel} onClick={() => setConfirming(true)}>
      <Trash2 size={16} />
      Delete
    </button>
  );
}

function ProgressIndicator({ post }: { post: Post }) {
  const progress = analysisProgress(post);
  if (!progress) return null;
  return (
    <div className="analysis-progress" aria-label="Analysis progress">
      <div className="progress-copy">
        <span>{progress.label}</span>
        <strong>{progress.percent}%</strong>
      </div>
      <div
        className="progress-track"
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={progress.percent}
      >
        <i style={{ width: `${progress.percent}%` }} />
      </div>
      <small>{progress.detail}</small>
    </div>
  );
}

function FileInput({ label, name, multiple = false }: { label: string; name: string; multiple?: boolean }) {
  const id = useId();
  const [fileNames, setFileNames] = useState<string[]>([]);
  return (
    <label htmlFor={id}>
      {label}
      <input
        id={id}
        className="visually-hidden-file"
        name={name}
        type="file"
        accept="image/png,image/jpeg,image/webp"
        multiple={multiple}
        required
        onChange={(event) => {
          setFileNames(Array.from(event.currentTarget.files ?? []).map((file) => file.name));
        }}
      />
      <span className="file-picker">
        <ImagePlus size={18} />
        <strong>{multiple ? "Select covers" : "Select cover"}</strong>
        <small>{fileNames.length ? fileNames.join(", ") : multiple ? "No candidates selected" : "No image selected"}</small>
      </span>
    </label>
  );
}

function PendingCopy({ status }: { status: Status }) {
  if (status === "failed") return null;
  return (
    <div className="pending-copy">
      <Loader2 className="spin" size={18} />
      Analysis can take a while while TRIBE v2 loads weights, extracts features, and runs inference.
    </div>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="metric">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function computeViralityPotential(summary: NonNullable<Post["analysis_summary"]>): number {
  const globalMean = Math.max(summary.metrics.global_mean_abs ?? 0, 1e-9);
  const rel = (key: string) => {
    const raw = (summary.networks[key]?.raw ?? 0);
    return Math.min(1, raw / (globalMean * 1.5));
  };
  const sustained = Math.min(1, (summary.metrics.sustained_ratio ?? 0) * 2);
  const score =
    0.28 * rel("social") +
    0.24 * rel("valuation") +
    0.22 * rel("attention") +
    0.12 * rel("visual") +
    0.06 * rel("memory_scene") +
    0.08 * sustained;
  return Math.min(1, Math.max(0, Math.round(score * 1000) / 1000));
}

function formatMetric(value?: number) {
  if (value == null) return "N/A";
  if (Math.abs(value) >= 100) return value.toFixed(0);
  if (Math.abs(value) >= 1) return value.toFixed(2);
  return value.toPrecision(3);
}

function buildInsightAnalytics(posts: Post[]): InsightAnalytics {
  const historical = posts.filter((post) => post.section === "historical");
  const postsWithLikes = historical.filter((post) => typeof post.likes === "number");
  const sortedByLikes = [...postsWithLikes].sort((left, right) => (right.likes ?? 0) - (left.likes ?? 0));
  const topPosts = sortedByLikes.slice(0, TOP_POST_LIMIT);
  const hookPosts = historical.filter((post) => hasHookText(post));
  const topPostsWithHooks = topPosts.filter((post) => hasHookText(post));
  const topLikes = topPosts.map((post) => post.likes ?? 0);

  return {
    historicalCount: historical.length,
    postsWithLikesCount: postsWithLikes.length,
    hooksAvailableCount: hookPosts.length,
    topLimit: TOP_POST_LIMIT,
    topPosts,
    topPostsWithHooksCount: topPostsWithHooks.length,
    topWordCloud: buildWordTerms(topPostsWithHooks, WORD_CLOUD_LIMIT),
    allWordCloud: buildWordTerms(hookPosts, WORD_CLOUD_LIMIT),
    topHooks: topPosts
      .map((post, index) => ({
        post,
        rank: index + 1,
        hook: compactText(post.hook_text || "", 220)
      }))
      .filter((row) => row.hook),
    phraseTerms: buildPhraseTerms(topPostsWithHooks, PHRASE_LIMIT),
    postTypeStats: buildGroupStats(postsWithLikes, (post) => splitLabels(post.post_type_label)),
    entityStats: buildGroupStats(postsWithLikes, (post) => [
      ...splitLabels(post.person_label),
      ...splitLabels(post.company_label)
    ]),
    avgTopLikes: average(topLikes),
    medianTopLikes: median(topLikes)
  };
}

function buildWordTerms(posts: Post[], limit: number): WordTerm[] {
  const counts = new Map<string, { count: number; posts: number }>();
  for (const post of posts) {
    const tokens = tokenizeHook(post.hook_text || "");
    const seen = new Set<string>();
    for (const token of tokens) {
      const current = counts.get(token) ?? { count: 0, posts: 0 };
      current.count += 1;
      if (!seen.has(token)) {
        current.posts += 1;
        seen.add(token);
      }
      counts.set(token, current);
    }
  }
  return normalizeWeightedTerms(
    [...counts.entries()]
      .map(([term, value]) => ({ term, count: value.count, posts: value.posts, weight: 0 }))
      .sort((left, right) => right.count - left.count || right.posts - left.posts || left.term.localeCompare(right.term))
      .slice(0, limit)
  );
}

function buildPhraseTerms(posts: Post[], limit: number): PhraseTerm[] {
  const counts = new Map<string, { count: number; posts: number }>();
  for (const post of posts) {
    const tokens = tokenizeHook(post.hook_text || "");
    const seen = new Set<string>();
    for (let index = 0; index < tokens.length - 1; index += 1) {
      const phrase = `${tokens[index]} ${tokens[index + 1]}`;
      const current = counts.get(phrase) ?? { count: 0, posts: 0 };
      current.count += 1;
      if (!seen.has(phrase)) {
        current.posts += 1;
        seen.add(phrase);
      }
      counts.set(phrase, current);
    }
  }
  const terms = [...counts.entries()]
    .map(([phrase, value]) => ({ phrase, count: value.count, posts: value.posts, weight: 0 }))
    .filter((item) => item.count >= 2)
    .sort((left, right) => right.count - left.count || right.posts - left.posts || left.phrase.localeCompare(right.phrase))
    .slice(0, limit);

  const max = Math.max(...terms.map((item) => item.count), 1);
  const min = Math.min(...terms.map((item) => item.count), max);
  return terms.map((item) => ({
    ...item,
    weight: max === min ? 0.65 : (item.count - min) / (max - min)
  }));
}

function buildGroupStats(posts: Post[], labelsForPost: (post: Post) => string[]): GroupStat[] {
  const groups = new Map<string, { count: number; likes: number; maxLikes: number; hooks: number }>();
  for (const post of posts) {
    const labels = labelsForPost(post);
    for (const label of labels) {
      const current = groups.get(label) ?? { count: 0, likes: 0, maxLikes: 0, hooks: 0 };
      const likes = post.likes ?? 0;
      current.count += 1;
      current.likes += likes;
      current.maxLikes = Math.max(current.maxLikes, likes);
      current.hooks += hasHookText(post) ? 1 : 0;
      groups.set(label, current);
    }
  }
  return [...groups.entries()]
    .map(([label, value]) => ({
      label,
      count: value.count,
      avgLikes: value.likes / Math.max(1, value.count),
      maxLikes: value.maxLikes,
      hookCoverage: value.hooks / Math.max(1, value.count)
    }))
    .filter((stat) => stat.count >= 2)
    .sort((left, right) => right.avgLikes - left.avgLikes || right.maxLikes - left.maxLikes)
    .slice(0, 8);
}

function normalizeWeightedTerms<T extends { count: number; weight: number }>(terms: T[]): T[] {
  const max = Math.max(...terms.map((term) => term.count), 1);
  const min = Math.min(...terms.map((term) => term.count), max);
  return terms.map((term) => ({
    ...term,
    weight: max === min ? 0.65 : (term.count - min) / (max - min)
  }));
}

function tokenizeHook(text: string): string[] {
  const normalized = text
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .replace(/https?:\/\/\S+/g, " ");
  const matches = normalized.match(/[a-z0-9]+/g) ?? [];
  return matches.flatMap((rawToken) => {
    const token = rawToken === "al" ? "ai" : rawToken;
    if (/^\d+$/.test(token)) return [];
    if (!KEEP_SHORT_WORDS.has(token) && token.length < 3) return [];
    if (STOP_WORDS.has(token)) return [];
    return [token];
  });
}

function hasHookText(post: Post) {
  return Boolean(post.hook_text?.trim());
}

function splitLabels(value?: string | null): string[] {
  if (!value) return [];
  return value
    .split(/[,;/|]+/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function compactText(value: string, limit: number) {
  const text = value.replace(/\s+/g, " ").trim();
  if (text.length <= limit) return text;
  return `${text.slice(0, Math.max(0, limit - 1)).trim()}...`;
}

function average(values: number[]) {
  if (!values.length) return 0;
  return values.reduce((sum, value) => sum + value, 0) / values.length;
}

function median(values: number[]) {
  if (!values.length) return 0;
  const sorted = [...values].sort((left, right) => left - right);
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2;
}

function formatPercent(value: number) {
  return `${Math.round(value * 100)}%`;
}

function formatCompactNumber(value: number) {
  return Intl.NumberFormat(undefined, {
    notation: "compact",
    maximumFractionDigits: value >= 10000 ? 1 : 0
  }).format(Math.round(value));
}

function abDecision(test: AbTest, candidates: Post[]): AbDecision {
  const winner = candidates.find((candidate) => candidate.id === test.winner_post_id) ?? null;
  const hasRunning = candidates.some((candidate) => candidate.status === "queued" || candidate.status === "running");
  if (hasRunning || test.status === "running") {
    return {
      winner: null,
      basis: "Pending",
      status: "waiting",
      confidence: "Pending",
      margin: "Pending",
      body: "The backend will choose a winner after all candidates finish."
    };
  }

  if (!winner || test.status === "failed") {
    return {
      winner: null,
      basis: "Unavailable",
      status: "failed",
      confidence: "N/A",
      margin: "N/A",
      body: "No completed candidate was available for selection."
    };
  }

  const completed = candidates
    .filter((candidate) => candidate.status === "completed")
    .sort((left, right) => (right.ranking_value ?? 0) - (left.ranking_value ?? 0));
  const runnerUp = completed.find((candidate) => candidate.id !== winner.id) ?? null;
  const basis = winner.ranking_basis === "calibrated_likes" ? "Calibrated likes" : "TRIBE activation";
  const margin = runnerUp
    ? formatDecisionMargin(winner, runnerUp)
    : "Only completed candidate";
  const confidence = winner.ranking_basis === "calibrated_likes" && winner.calibrated_prediction
    ? `${Math.round(winner.calibrated_prediction.confidence * 100)}%`
    : "Uncalibrated";
  const body = winner.ranking_basis === "calibrated_likes"
    ? "The app selected the candidate with the strongest locally calibrated predicted performance."
    : "The app selected the candidate with the strongest TRIBE v2 global activation because local likes calibration is not ready yet.";

  return {
    winner,
    basis,
    status: "chosen",
    confidence,
    margin,
    body
  };
}

function formatDecisionMargin(winner: Post, runnerUp: Post) {
  const winnerValue = winner.ranking_value ?? 0;
  const runnerUpValue = runnerUp.ranking_value ?? 0;
  const delta = winnerValue - runnerUpValue;
  if (winner.ranking_basis === "calibrated_likes") {
    return `+${Math.max(0, Math.round(delta)).toLocaleString()} likes`;
  }
  if (runnerUpValue <= 0) return "Clear lead";
  return `+${Math.max(0, (delta / runnerUpValue) * 100).toFixed(1)}%`;
}

function interpretPost(post: Post): Interpretation | null {
  const summary = post.analysis_summary;
  if (!summary) return null;

  const rankedNetworks = Object.entries(summary.networks)
    .map(([key, network]) => ({
      key,
      label: networkLabels[key] ?? network.label,
      score: network.score ?? 0
    }))
    .sort((left, right) => right.score - left.score);
  const primary = rankedNetworks[0];
  const secondary = rankedNetworks[1];
  const topRegion = summary.top_regions[0];
  const metrics = summary.metrics;
  const sustainedRatio = metrics.sustained_ratio ?? 0;
  const lateMinusEarly = metrics.late_minus_early ?? 0;
  const meanActivation = Math.max(metrics.global_mean_abs ?? 0, 1e-9);
  const lateShift = lateMinusEarly / meanActivation;
  const zeroNetworkCount = rankedNetworks.filter((network) => network.score <= 0).length;
  const temporal = temporalInterpretation(sustainedRatio, lateShift);
  const historical = historicalInterpretation(post);

  const headline = post.calibrated_prediction
    ? "Performance read"
    : "Neural read";
  const summaryText = post.calibrated_prediction
    ? `The local model estimates ${post.calibrated_prediction.predicted_likes.toLocaleString()} likes with ${Math.round(post.calibrated_prediction.confidence * 100)}% confidence.`
    : "This is a neural response profile, not a like prediction yet. Add historical posts with likes to calibrate it.";

  const points: InterpretationPoint[] = [
    {
      label: "Dominant response",
      value: primary ? `${primary.label} ${Math.round(primary.score)}` : "N/A",
      body: primary
        ? `The strongest modeled network is associated with ${networkMeanings[primary.key] ?? "the matching HCP network group"}.`
        : "No dominant network was available in this summary."
    },
    {
      label: "Secondary signal",
      value: secondary ? `${secondary.label} ${Math.round(secondary.score)}` : "N/A",
      body: secondary
        ? `The next strongest signal points to ${networkMeanings[secondary.key] ?? "another network group"}, which can explain why the cover feels more contextual than purely visual.`
        : "A secondary network was not available."
    },
    {
      label: "Temporal shape",
      value: temporal.value,
      body: temporal.body
    },
    {
      label: "Archive context",
      value: historical.value,
      body: historical.body
    }
  ];

  const regionText = topRegion
    ? ` Top HCP region: ${topRegion.name} (${Math.round(topRegion.score)} relative score).`
    : "";
  const staleNetworkText = zeroNetworkCount >= 3
    ? " Several networks are still 0 in this saved result; reanalyze it to refresh the full-atlas network scoring."
    : "";
  const caveat = `Use this as a ranking and comparison signal, not as a literal diagnosis of what a viewer thinks.${regionText}${staleNetworkText}`;

  return {
    headline,
    summary: summaryText,
    points,
    caveat
  };
}

function temporalInterpretation(sustainedRatio: number, lateShift: number) {
  if (lateShift > 0.12) {
    return {
      value: "Builds over time",
      body: "Later segments are stronger than early segments, suggesting the cover/video representation gains response after initial exposure."
    };
  }
  if (lateShift < -0.12) {
    return {
      value: "Front-loaded",
      body: "Early response is stronger than later response, so the cover may land quickly but lose intensity across the short clip."
    };
  }
  if (sustainedRatio >= 0.18) {
    return {
      value: "Sustained",
      body: "Activation is relatively steady instead of being driven by only one sharp peak."
    };
  }
  return {
    value: "Peak-driven",
    body: "The response is concentrated in sharper peaks, so the strongest areas matter more than the average alone."
  };
}

function historicalInterpretation(post: Post) {
  if (post.tribe_percentile != null) {
    if (post.tribe_percentile >= 75) {
      return {
        value: `P${post.tribe_percentile}`,
        body: "This ranks high against your analyzed historical archive."
      };
    }
    if (post.tribe_percentile <= 35) {
      return {
        value: `P${post.tribe_percentile}`,
        body: "This ranks low against your current historical archive."
      };
    }
    return {
      value: `P${post.tribe_percentile}`,
      body: "This sits around the middle of your current historical archive."
    };
  }
  return {
    value: "Not calibrated",
    body: "There are not enough historical posts with likes to compare this score against your own audience."
  };
}

function analysisProgress(post: Post): { percent: number; label: string; detail: string } | null {
  if (post.status === "completed" || post.status === "failed") return null;

  const backendPercent = typeof post.progress_percent === "number" ? post.progress_percent : null;
  if (backendPercent !== null && backendPercent > 0) {
    const percent = post.status === "running" && backendPercent < 94
      ? estimatedPhaseProgress(backendPercent, elapsedSince(post.updated_at))
      : backendPercent;
    return {
      percent: clampProgress(percent),
      label: post.progress_message || (post.status === "queued" ? "Queued" : "Analyzing with TRIBE v2"),
      detail: runningDetail(post)
    };
  }

  if (post.status === "queued") {
    return {
      percent: 8,
      label: "Queued",
      detail: "Waiting for the backend to start analysis."
    };
  }

  const elapsedSeconds = elapsedSince(post.updated_at || post.created_at);
  const estimatedPercent = Math.min(94, 18 + Math.floor((elapsedSeconds / 300) * 76));
  return {
    percent: clampProgress(estimatedPercent),
    label: "TRIBE v2 running on CPU",
    detail: `${formatElapsed(elapsedSeconds)} elapsed. This is estimated because TRIBE v2 does not expose internal frame-level progress.`
  };
}

function runningDetail(post: Post) {
  if (post.status !== "running") return "The backend will update this automatically.";
  return `${formatElapsed(elapsedSince(post.updated_at || post.created_at))} in the current phase.`;
}

function elapsedSince(value?: string) {
  if (!value) return 0;
  const timestamp = new Date(value).getTime();
  if (Number.isNaN(timestamp)) return 0;
  return Math.max(0, Math.floor((Date.now() - timestamp) / 1000));
}

function formatElapsed(seconds: number) {
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  return remainder ? `${minutes}m ${remainder}s` : `${minutes}m`;
}

function clampProgress(value: number) {
  return Math.max(0, Math.min(100, Math.round(value)));
}

function estimatedPhaseProgress(basePercent: number, secondsInPhase: number) {
  const nextCap = basePercent >= 84 ? 94 : 82;
  const phaseGain = Math.floor((secondsInPhase / 300) * (nextCap - basePercent));
  return Math.min(nextCap, basePercent + Math.max(0, phaseGain));
}

export default CortexSurfaceApp;
