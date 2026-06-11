import {
  Activity,
  BarChart3,
  Brain,
  CheckCircle2,
  CircleAlert,
  Cloud,
  Database,
  FileText,
  FlaskConical,
  Hash,
  ImagePlus,
  ListChecks,
  Loader2,
  Lock,
  Pencil,
  RefreshCcw,
  Save,
  Sparkles,
  Target,
  Trash2,
  Trophy,
  Upload,
  X,
  Zap
} from "lucide-react";
import { ChangeEvent, FormEvent, useEffect, useId, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import * as THREE from "three";
import {
  API_BASE,
  analyzePost,
  createAbTest,
  createBatchPosts,
  createPost,
  createPostFromInstagramLink,
  deleteAbTest,
  deletePost,
  generatePostReport,
  getAbTest,
  getAbTests,
  getCalibration,
  getHealth,
  getMetadataOptions,
  getPost,
  getPosts,
  mediaUrl,
  updatePost
} from "./cortexRunApi";
import { getApiKey, setApiKey } from "./auth";
import { runModalOcrBatch } from "./cortexRunOcrApi";
import fsaverageMesh from "./assets/fsaverage5-pial.json";
import type {
  AbTest,
  Calibration,
  Health,
  LlmReport,
  MetadataOptions,
  NetworkScore,
  Post,
  Status,
  TemporalPoint
} from "./types";

type Tab = "analyze" | "history" | "insights" | "ab";
type InsightTab = "hooks" | "top" | "patterns";
type PostDbSortKey = "newest" | "oldest" | "likes" | "comments" | "brain_mean" | "brain_peak" | "virality";
type PostDbFilterKey = "all" | "analyzed" | "needs_analysis" | "has_hook" | "carousel" | "image" | "video";

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
  { id: "history", label: "Post DB", icon: Database },
  { id: "insights", label: "Insights", icon: Sparkles },
  { id: "ab", label: "A/B Testing", icon: FlaskConical }
];

const insightTabs: Array<{ id: InsightTab; label: string; icon: typeof Brain }> = [
  { id: "hooks", label: "Hooks", icon: Cloud },
  { id: "top", label: "Top 20", icon: Trophy },
  { id: "patterns", label: "Patterns", icon: Hash }
];

const defaultMetadataOptions: MetadataOptions = {
  people: ["Elon Musk", "Sam Altman", "Jensen Huang", "Dario Amodei", "Donald Trump", "Xi Jinping"],
  companies: ["ChatGPT / OpenAI", "Claude / Anthropic", "Gemini / Google", "Grok / xAI"],
  post_types: ["Tricks", "News", "Promo", "Reel", "Meme"],
  tags: ["openai", "claude", "grok", "round-sticker"]
};

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

const FLOP_LIKES_BASELINE = 850;

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

type PostDbBatchRow = {
  id: string;
  file: File;
  title: string;
  publishedAt: string;
  likes: string;
  personLabel: string;
  companyLabel: string;
  postTypeLabel: string;
  caption: string;
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
  avgComments: number;
  maxComments: number;
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
  avgTopComments: number;
  medianTopComments: number;
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

const MAX_ACTIVE_BRAIN_RENDERERS = 4;
let activeBrainRenderers = 0;

function CortexRunApp() {
  const [activeTab, setActiveTab] = useState<Tab>("analyze");
  const [health, setHealth] = useState<Health | null>(null);
  const [calibration, setCalibration] = useState<Calibration | null>(null);
  const [singlePosts, setSinglePosts] = useState<Post[]>([]);
  const [historicalPosts, setHistoricalPosts] = useState<Post[]>([]);
  const [abTests, setAbTests] = useState<AbTest[]>([]);
  const [selectedTest, setSelectedTest] = useState<{ test: AbTest; candidates: Post[] } | null>(null);
  const [metadataOptions, setMetadataOptions] = useState<MetadataOptions>(defaultMetadataOptions);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const insights = useMemo(() => buildInsightAnalytics(historicalPosts), [historicalPosts]);

  async function refresh() {
    setError(null);
    const healthResult = await getHealth();
    setHealth(healthResult);
    getPosts("historical")
      .then((historicalResult) => setHistoricalPosts(historicalResult.posts))
      .catch((caught) => setError(caught instanceof Error ? caught.message : "Could not load Post DB."));
    const [calibrationResult, singleResult, testsResult, metadataResult] = await Promise.all([
      getCalibration(),
      getPosts("single"),
      getAbTests(),
      getMetadataOptions()
    ]);
    setCalibration(calibrationResult);
    setSinglePosts(singleResult.posts);
    setAbTests(testsResult.tests);
    setMetadataOptions(metadataResult);
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
    return posts.some(isActivelyAnalyzing);
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
      if (section === "historical") {
        form.set("analyze_now", "false");
      }
      normalizePostForm(form);

      await createPost(form);
      formElement.reset();
      await refresh();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not create the analysis.");
    } finally {
      setLoading(false);
    }
  }

  async function submitInstagramLink(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const formElement = event.currentTarget;
    setLoading(true);
    setError(null);
    try {
      const form = new FormData(formElement);
      await createPostFromInstagramLink(form);
      formElement.reset();
      await refresh();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not import that Instagram post.");
    } finally {
      setLoading(false);
    }
  }

  async function submitPostDbBatch(form: FormData) {
    setLoading(true);
    setError(null);
    try {
      await createBatchPosts(form);
      await refresh();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not create the Post DB batch.");
    } finally {
      setLoading(false);
    }
  }

  async function handleUpdateLikes(post: Post, likes: number) {
    setError(null);
    try {
      const form = new FormData();
      form.set("likes", String(likes));
      await updatePost(post.id, form);
      await refresh();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not update likes.");
    }
  }

  async function handleUpdatePost(post: Post, form: FormData) {
    setError(null);
    try {
      normalizePostForm(form);
      await updatePost(post.id, form);
      await refresh();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not update Post DB metadata.");
      throw caught;
    }
  }

  async function handleRunModalOcrBatch() {
    setLoading(true);
    setError(null);
    try {
      await runModalOcrBatch();
      await refresh();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not run Modal OCR batch.");
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
          <p className="product-name">Sentient</p>
          <h1>Cortex</h1>
          <p className="topbar-subtitle">Neural performance analysis for visual covers</p>
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
        <section className="workspace-grid analyze-workspace">
          <div className="stack">
            <InstagramLinkPanel onSubmit={submitInstagramLink} loading={loading} />
            <UploadPanel
              title="New cover"
              description="Upload an image. The backend turns it into a short silent MP4 before sending it to TRIBE v2."
              onSubmit={(event) => submitPost(event, "single")}
              loading={loading}
              metadataOptions={metadataOptions}
            />
          </div>
          <ResultsColumn
            posts={singlePosts}
            empty="No analyzed covers in this section yet."
            onAnalyze={async (post) => {
              await analyzePost(post.id);
              await refresh();
            }}
            onDelete={handleDeletePost}
            onUpdateLikes={handleUpdateLikes}
          />
        </section>
      ) : null}

      {activeTab === "history" ? (
        <section className="workspace-grid wide-left">
          <div className="stack">
            <CalibrationPanel calibration={calibration} />
            <PostDbOcrPanel
              posts={historicalPosts}
              health={health}
              loading={loading}
              onRun={handleRunModalOcrBatch}
            />
            <UploadPanel
              title="Post DB post"
              description="Add published covers and real likes as source data. Analysis is manual so imports do not spend GPU until you choose."
              onSubmit={(event) => submitPost(event, "historical")}
              loading={loading}
              isPostDb
              submitLabel="Add to Post DB"
              metadataOptions={metadataOptions}
            />
          </div>
          <PostDbGridPanel
            posts={historicalPosts}
            empty="Post DB is empty. Import covers and real likes before analyzing patterns."
            onAnalyze={async (post) => {
              await analyzePost(post.id);
              await refresh();
            }}
            onDelete={handleDeletePost}
            onUpdatePost={handleUpdatePost}
            metadataOptions={metadataOptions}
          />
        </section>
      ) : null}

      {activeTab === "insights" ? (
        <InsightsPanel insights={insights} />
      ) : null}

      {activeTab === "ab" ? (
        <section className="workspace-grid ab-workspace">
          <div className="stack">
            <AbUploadPanel onSubmit={submitAbTest} loading={loading} metadataOptions={metadataOptions} />
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
          <Metric label="Avg comments" value={formatCompactNumber(insights.avgTopComments)} />
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
                <strong>{(post.likes ?? 0).toLocaleString()} likes</strong>
                <strong>{(post.comments ?? 0).toLocaleString()} comments</strong>
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
          <div style={{ display: "flex", flexDirection: "column", alignItems: "flex-end", gap: "2px" }}>
            <span>{formatCompactNumber(stat.avgLikes)} likes</span>
            <span className="muted" style={{ fontSize: "0.8rem" }}>{formatCompactNumber(stat.avgComments)} cmts</span>
          </div>
        </div>
      ))}
    </div>
  );
}

function HealthBanner({ health }: { health: Health | null }) {
  if (!health) return <div className="status-strip muted">Checking backend connection...</div>;
  const tribe = health.tribev2;
  const remoteTribe = Boolean(health.remote_tribe?.configured);
  if (!tribe.installed && !remoteTribe) {
    return (
      <div className="status-strip warning">
        <CircleAlert size={18} />
        TRIBE v2 is not installed in the backend and no remote GPU is configured. The app cannot generate results until one is available.
      </div>
    );
  }
  return (
    <div className="status-strip">
      <CheckCircle2 size={18} />
      <span>{tribe.installed ? "TRIBE v2 installed" : "TRIBE v2 via remote GPU"}</span>
      <span>Model: {tribe.model_id}</span>
      <span>Device: {tribe.device}</span>
      <span>{tribe.hf_token_present ? "HF token detected" : "HF token missing"}</span>
      <span>{health.remote_tribe?.configured ? "Remote GPU enabled" : "Local TRIBE mode"}</span>
      <span>{health.remote_ocr?.configured ? "Modal OCR enabled" : "Modal OCR not configured"}</span>
      <span>Report LLM: {health.llm_report?.model_id ?? "Not configured"}</span>
    </div>
  );
}

function UploadPanel({
  title,
  description,
  onSubmit,
  loading,
  metadataOptions,
  isPostDb = false,
  submitLabel = "Analyze with TRIBE v2"
}: {
  title: string;
  description: string;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
  loading: boolean;
  metadataOptions: MetadataOptions;
  isPostDb?: boolean;
  submitLabel?: string;
}) {
  return (
    <form className={`panel upload-panel ${isPostDb ? "postdb-upload form-workflow" : "analyze-upload form-workflow"}`} onSubmit={onSubmit}>
      <div className="panel-title">
        <ImagePlus size={20} />
        <div>
          <h2>{title}</h2>
          <p>{description}</p>
        </div>
      </div>

      {isPostDb ? (
        <>
          <section className="analyze-section postdb-section">
            <FormStepHeader
              step="01"
              title="Published asset"
              description="Add the real cover and the basic publishing info for this historical post."
            />
            <div className="form-card-fields">
              <label>
                Title
                <input name="title" placeholder="Friday release cover" required />
                <small className="field-hint">Use the post topic or hook so it scans cleanly in Post DB.</small>
              </label>
              <FileInput label="Cover image" name="file" hint="PNG, JPG, or WebP. This is stored as the Post DB cover." />
              <label>
                Publish date
                <input name="published_at" type="date" />
              </label>
            </div>
          </section>

          <section className="analyze-section postdb-section">
            <FormStepHeader
              step="02"
              title="Performance"
              description="Real engagement is what calibrates the prediction model."
            />
            <div className="form-row">
              <label>
                Real likes
                <input name="likes" type="number" min="0" placeholder={`${FLOP_LIKES_BASELINE} for flop`} />
                <small className="field-hint">Blank saves as {FLOP_LIKES_BASELINE} flop likes.</small>
              </label>
              <label>
                Comments
                <input name="comments" type="number" min="0" placeholder="Number of comments" />
                <small className="field-hint">Optional, but useful for sorting patterns.</small>
              </label>
            </div>
            <div className="form-row">
              <label>
                Video duration
                <input name="duration_seconds" type="number" min="2" max="10" defaultValue="2" />
                <small className="field-hint">2 seconds is enough for static covers.</small>
              </label>
              <label className="checkbox-field checkbox-field-inline">
                <span>Animated original</span>
                <input name="is_animated" type="checkbox" />
              </label>
            </div>
          </section>

          <section className="analyze-section postdb-section">
            <FormStepHeader
              step="03"
              title="Content context"
              description="Labels, hook text, and notes make Post DB searchable and easier to compare later."
            />
            <label>
              Hook
              <input name="hook_text" placeholder="Manual hook text; Modal OCR fills blank hooks later" />
            </label>
            <label>
              Caption / Notes
              <textarea name="caption" rows={3} placeholder="Post context, song, design direction, or hypothesis" />
            </label>
            <MetadataInputs metadataOptions={metadataOptions} showSuggestions={false} compact />
            <TagsInput
              name="tags"
              label="Tags"
              placeholder="openai, flag-us, round-sticker"
              options={metadataOptions.tags || []}
              showSuggestions={false}
            />
          </section>
        </>
      ) : (
        <>
          <section className="analyze-section">
            <FormStepHeader
              step="01"
              title="Creative asset"
              description="Name the concept and add the cover you want the model to score."
            />
            <div className="form-card-fields">
              <label>
                Title
                <input name="title" placeholder="Friday release cover" required />
                <small className="field-hint">Use a short working name so it is easy to find in results.</small>
              </label>
              <FileInput label="Cover image" name="file" hint="PNG, JPG, or WebP. Square covers work best in the results grid." />
            </div>
          </section>

          <section className="analyze-section">
            <FormStepHeader
              step="02"
              title="Run settings"
              description="Keep the default duration for static covers; only change it when the original post was animated."
            />
            <div className="form-row analyze-settings-row">
              <label>
                Video duration
                <input name="duration_seconds" type="number" min="2" max="10" defaultValue="2" />
                <small className="field-hint">Range: 2-10 seconds.</small>
              </label>
              <label className="checkbox-field checkbox-field-inline">
                <span>Animated original</span>
                <input name="is_animated" type="checkbox" />
              </label>
            </div>
          </section>

          <section className="analyze-section">
            <FormStepHeader
              step="03"
              title="Context signals"
              description="Optional fields help grouping later, but they do not block the analysis."
            />
            <div className="form-row">
              <label>
                Publish date
                <input name="published_at" type="date" />
              </label>
              <label>
                Hook
                <input name="hook_text" placeholder="Optional hook text override" />
              </label>
            </div>
            <label>
              Caption / Notes
              <textarea name="caption" rows={3} placeholder="Optional context for this concept" />
            </label>
            <MetadataInputs metadataOptions={metadataOptions} showSuggestions={false} compact />
            <TagsInput
              name="tags"
              label="Tags"
              placeholder="Comma-separated (e.g., openai, flag-us, round-sticker)"
              options={metadataOptions.tags || []}
              showSuggestions={false}
            />
          </section>
        </>
      )}

      <FormSubmitBar
        loading={loading}
        label={submitLabel}
        helper={isPostDb ? "Saves this cover into Post DB without spending GPU automatically." : "Creates a silent MP4 and starts TRIBE v2 analysis."}
      />
    </form>
  );
}

function FormStepHeader({
  step,
  title,
  description
}: {
  step: string;
  title: string;
  description: string;
}) {
  return (
    <div className="form-step-head">
      <span>{step}</span>
      <div>
        <h3>{title}</h3>
        <p>{description}</p>
      </div>
    </div>
  );
}

function FormSubmitBar({
  loading,
  label,
  helper
}: {
  loading: boolean;
  label: string;
  helper: string;
}) {
  return (
    <div className="form-submit-bar">
      <small>{helper}</small>
      <button className="primary-button" disabled={loading}>
        {loading ? <Loader2 className="spin" size={18} /> : <Upload size={18} />}
        {label}
      </button>
    </div>
  );
}

function MetadataInputs({
  metadataOptions,
  values,
  onChange,
  showSuggestions = true,
  compact = false
}: {
  metadataOptions: MetadataOptions;
  values?: {
    personLabel?: string;
    companyLabel?: string;
    postTypeLabel?: string;
  };
  onChange?: (patch: { personLabel?: string; companyLabel?: string; postTypeLabel?: string }) => void;
  showSuggestions?: boolean;
  compact?: boolean;
}) {
  return (
    <div className="metadata-grid">
      <TagsInput
        label="Person / subject"
        name="person_label"
        options={metadataOptions.people || []}
        placeholder={compact ? "Sam Altman, Satya Nadella" : "Comma-separated (e.g., Sam Altman, Satya Nadella)"}
        value={values?.personLabel}
        onChange={(personLabel) => onChange?.({ personLabel })}
        showSuggestions={showSuggestions}
      />
      <TagsInput
        label="Company / product"
        name="company_label"
        options={metadataOptions.companies || []}
        placeholder={compact ? "OpenAI, Microsoft" : "Comma-separated (e.g., OpenAI, Microsoft)"}
        value={values?.companyLabel}
        onChange={(companyLabel) => onChange?.({ companyLabel })}
        showSuggestions={showSuggestions}
      />
      <CreatableMetadataInput
        label="Post type"
        name="post_type_label"
        options={metadataOptions.post_types}
        placeholder={compact ? "Tricks, News, Meme" : "Tricks, News, Meme..."}
        value={values?.postTypeLabel}
        onChange={(postTypeLabel) => onChange?.({ postTypeLabel })}
      />
    </div>
  );
}

function CreatableMetadataInput({
  label,
  name,
  options,
  placeholder,
  value,
  onChange
}: {
  label: string;
  name: string;
  options: string[];
  placeholder: string;
  value?: string;
  onChange?: (value: string) => void;
}) {
  const [draftValue, setDraftValue] = useState(value ?? "");
  useEffect(() => {
    setDraftValue(value ?? "");
  }, [value]);
  const listId = useId();
  return (
    <label>
      {label}
      <input
        name={name}
        list={listId}
        value={draftValue}
        placeholder={placeholder}
        onChange={(event) => {
          setDraftValue(event.currentTarget.value);
          onChange?.(event.currentTarget.value);
        }}
      />
      <datalist id={listId}>
        {options.map((option) => (
          <option value={option} key={option} />
        ))}
      </datalist>
      <small className="field-hint">Choose one or type a new value.</small>
    </label>
  );
}

function TagsInput({ 
  name, 
  label, 
  placeholder, 
  options,
  value,
  onChange,
  showSuggestions = true
}: { 
  name: string; 
  label: string; 
  placeholder: string; 
  options: string[];
  value?: string;
  onChange?: (value: string) => void;
  showSuggestions?: boolean;
}) {
  const inputRef = useRef<HTMLInputElement>(null);

  // If a default value is provided (like in inline editing), set it initially.
  // We use an effect so it doesn't fight with user input during normal form use.
  useEffect(() => {
    if (value !== undefined && inputRef.current) {
      inputRef.current.value = value;
    }
  }, [value]);

  function appendTag(tag: string) {
    if (!inputRef.current) return;
    const current = inputRef.current.value.split(",").map(t => t.trim()).filter(Boolean);
    if (!current.includes(tag)) {
      current.push(tag);
      inputRef.current.value = current.join(", ") + (current.length ? ", " : "");
      onChange?.(inputRef.current.value);
    }
  }

  return (
    <div className="tags-input-container">
      <label>
        {label}
        <input 
          ref={inputRef}
          name={name} 
          placeholder={placeholder} 
          onChange={(event) => onChange?.(event.currentTarget.value)}
        />
      </label>
      {showSuggestions && options.length > 0 && (
        <div className="tag-suggestions">
          {options.map(tag => (
            <button 
              key={tag} 
              type="button" 
              className="metadata-chip"
              onClick={() => appendTag(tag)}
            >
              + {tag}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function ResultsColumn({
  posts,
  empty,
  onAnalyze,
  onDelete,
  onUpdateLikes,
  onUpdatePost,
  metadataOptions = defaultMetadataOptions
}: {
  posts: Post[];
  empty: string;
  onAnalyze?: (post: Post) => void;
  onDelete: (post: Post) => Promise<void>;
  onUpdateLikes?: (post: Post, likes: number) => Promise<void>;
  onUpdatePost?: (post: Post, form: FormData) => Promise<void>;
  metadataOptions?: MetadataOptions;
}) {
  const [visibleCount, setVisibleCount] = useState(20);

  return (
    <div className="stack">
      {posts.length === 0 ? <div className="empty-state">{empty}</div> : null}
      {posts.slice(0, visibleCount).map((post) => (
        <PostResult
          key={post.id}
          post={post}
          onAnalyze={onAnalyze}
          onDelete={onDelete}
          onUpdateLikes={onUpdateLikes}
          onUpdatePost={onUpdatePost}
          metadataOptions={metadataOptions}
        />
      ))}
      {visibleCount < posts.length ? (
        <button className="secondary-button" onClick={() => setVisibleCount(v => v + 50)}>
          Load more ({posts.length - visibleCount} remaining)
        </button>
      ) : null}
    </div>
  );
}

function PostResult({
  post,
  onAnalyze,
  onDelete,
  onUpdateLikes,
  onUpdatePost,
  metadataOptions
}: {
  post: Post;
  onAnalyze?: (post: Post) => void;
  onDelete: (post: Post) => Promise<void>;
  onUpdateLikes?: (post: Post, likes: number) => Promise<void>;
  onUpdatePost?: (post: Post, form: FormData) => Promise<void>;
  metadataOptions: MetadataOptions;
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
            <PostTags post={post} />
          </div>
          <div className="result-actions">
            <StatusPill post={post} />
            <DeleteAction
              ariaLabel={`Delete ${post.title}`}
              confirmText="Delete this result and its files?"
              onConfirm={() => onDelete(post)}
            />
          </div>
        </div>
        <ProgressIndicator post={post} />
        {post.error ? <div className="inline-error">{post.error}</div> : null}
        {onAnalyze && canAnalyzePost(post) ? (
          <button className="text-button reanalyze-button" onClick={() => onAnalyze(post)}>
            <Activity size={16} />
            {post.status === "completed" ? "Reanalyze" : "Analyze"}
          </button>
        ) : null}
        {post.section === "historical" && onUpdatePost ? (
          <PostDbMetadataEditor
            post={post}
            metadataOptions={metadataOptions}
            onSave={(form) => onUpdatePost(post, form)}
          />
        ) : null}
        {post.section === "single" && onUpdateLikes ? (
          <LikesEditor
            post={post}
            onSave={(likes) => onUpdateLikes(post, likes)}
            buttonLabel="Save to Post DB"
            helperText="Saving real likes moves this cover into Post DB automatically."
          />
        ) : null}
        {post.section === "historical" ? <PostDbContentPanel post={post} /> : null}
        {post.analysis_summary ? <BrainSummary post={post} /> : <PendingCopy post={post} />}
      </div>
    </article>
  );
}

function PostDbGridPanel({
  posts,
  empty,
  onAnalyze,
  onDelete,
  onUpdatePost,
  metadataOptions
}: {
  posts: Post[];
  empty: string;
  onAnalyze: (post: Post) => void;
  onDelete: (post: Post) => Promise<void>;
  onUpdatePost: (post: Post, form: FormData) => Promise<void>;
  metadataOptions: MetadataOptions;
}) {
  const [visibleCount, setVisibleCount] = useState(30);
  const [selectedPostId, setSelectedPostId] = useState<number | null>(null);
  const [selectedPostDetail, setSelectedPostDetail] = useState<Post | null>(null);
  const [sortKey, setSortKey] = useState<PostDbSortKey>("newest");
  const [filterKey, setFilterKey] = useState<PostDbFilterKey>("all");
  const [query, setQuery] = useState("");

  const visiblePosts = useMemo(() => {
    const normalizedQuery = query.trim().toLowerCase();
    return posts
      .filter((post) => postMatchesPostDbFilter(post, filterKey))
      .filter((post) => {
        if (!normalizedQuery) return true;
        return [
          post.title,
          post.shortcode,
          post.person_label,
          post.company_label,
          post.post_type_label,
          post.hook_text
        ].some((value) => String(value ?? "").toLowerCase().includes(normalizedQuery));
      })
      .sort((left, right) => comparePostDbPosts(left, right, sortKey));
  }, [filterKey, posts, query, sortKey]);

  const selectedPost = useMemo(
    () => selectedPostDetail ?? posts.find((post) => post.id === selectedPostId) ?? null,
    [posts, selectedPostDetail, selectedPostId]
  );

  useEffect(() => {
    if (selectedPostId == null) return;
    if (!posts.some((post) => post.id === selectedPostId)) {
      setSelectedPostId(null);
      setSelectedPostDetail(null);
    }
  }, [posts, selectedPostId]);

  return (
    <section className="panel postdb-grid-panel">
      <div className="panel-title">
        <Database size={20} />
        <div>
          <h2>Post DB library</h2>
          <p>{posts.length.toLocaleString()} saved covers. Click a card to open full details.</p>
        </div>
      </div>
      <div className="postdb-controls">
        <label>
          Search
          <input
            type="search"
            value={query}
            onChange={(event) => {
              setQuery(event.currentTarget.value);
              setVisibleCount(30);
            }}
            placeholder="Title, shortcode, person, hook"
          />
        </label>
        <label>
          Filter
          <select
            value={filterKey}
            onChange={(event) => {
              setFilterKey(event.currentTarget.value as PostDbFilterKey);
              setVisibleCount(30);
            }}
          >
            <option value="all">All posts</option>
            <option value="analyzed">Analyzed</option>
            <option value="needs_analysis">Needs analysis</option>
            <option value="has_hook">Has hook</option>
            <option value="carousel">Carousel</option>
            <option value="image">Image</option>
            <option value="video">Video</option>
          </select>
        </label>
        <label>
          Sort
          <select
            value={sortKey}
            onChange={(event) => {
              setSortKey(event.currentTarget.value as PostDbSortKey);
              setVisibleCount(30);
            }}
          >
            <option value="newest">Newest</option>
            <option value="oldest">Oldest</option>
            <option value="likes">Most likes</option>
            <option value="comments">Most comments</option>
            <option value="brain_mean">Highest brain mean</option>
            <option value="brain_peak">Highest brain peak</option>
            <option value="virality">Highest signal</option>
          </select>
        </label>
        <span>{visiblePosts.length.toLocaleString()} shown</span>
      </div>
      {posts.length === 0 ? <div className="empty-state flat">{empty}</div> : null}
      {posts.length > 0 && visiblePosts.length === 0 ? <div className="empty-state flat">No posts match these filters.</div> : null}
      <div className="postdb-grid">
        {visiblePosts.slice(0, visibleCount).map((post) => (
          <button
            key={post.id}
            type="button"
            className="postdb-grid-card"
            onClick={() => {
              setSelectedPostId(post.id);
              setSelectedPostDetail(post);
              getPost(post.id)
                .then((result) => setSelectedPostDetail(result.post))
                .catch(() => setSelectedPostDetail(post));
            }}
          >
            <div className="postdb-grid-cover">
              {post.image_url ? <img src={mediaUrl(post.image_url)} alt={post.title} /> : null}
              <StatusPill post={post} />
            </div>
            <div className="postdb-grid-copy">
              <strong>{post.title}</strong>
              <p>{post.published_at || "No publish date"}</p>
              <span>{(post.likes ?? FLOP_LIKES_BASELINE).toLocaleString()} likes</span>
              <small>
                {(post.comments ?? 0).toLocaleString()} comments
                {post.brain_global_mean_abs != null ? ` · brain ${formatMetric(post.brain_global_mean_abs)}` : ""}
              </small>
            </div>
          </button>
        ))}
      </div>
      {visibleCount < visiblePosts.length ? (
        <button className="secondary-button" onClick={() => setVisibleCount((value) => value + 30)}>
          Load more ({visiblePosts.length - visibleCount} remaining)
        </button>
      ) : null}

      {selectedPost ? (
        <PostDbDetailModal
          post={selectedPost}
          onClose={() => {
            setSelectedPostId(null);
            setSelectedPostDetail(null);
          }}
          onAnalyze={onAnalyze}
          onDelete={onDelete}
          onUpdatePost={onUpdatePost}
          metadataOptions={metadataOptions}
        />
      ) : null}
    </section>
  );
}

function PostDbDetailModal({
  post,
  onClose,
  onAnalyze,
  onDelete,
  onUpdatePost,
  metadataOptions
}: {
  post: Post;
  onClose: () => void;
  onAnalyze: (post: Post) => void;
  onDelete: (post: Post) => Promise<void>;
  onUpdatePost: (post: Post, form: FormData) => Promise<void>;
  metadataOptions: MetadataOptions;
}) {
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  return createPortal(
    <div className="postdb-modal-backdrop" onClick={(event) => {
      if (event.target === event.currentTarget) onClose();
    }}>
      <section className="postdb-modal" role="dialog" aria-modal="true" aria-label={`${post.title} details`}>
        <header className="postdb-modal-header">
          <div>
            <h3>{post.title}</h3>
            <p>{post.published_at || "No publish date"} · {(post.likes ?? FLOP_LIKES_BASELINE).toLocaleString()} likes</p>
          </div>
          <button type="button" className="icon-button" aria-label="Close details" onClick={onClose}>
            <X size={16} />
          </button>
        </header>
        <div className="postdb-modal-actions">
          {canAnalyzePost(post) ? (
            <button className="text-button compact-action" onClick={() => onAnalyze(post)}>
              <Activity size={15} />
              {post.status === "completed" ? "Reanalyze" : "Analyze"}
            </button>
          ) : null}
          <StatusPill post={post} />
          <DeleteAction
            ariaLabel={`Delete ${post.title}`}
            confirmText="Delete this post and files?"
            onConfirm={async () => {
              await onDelete(post);
              onClose();
            }}
          />
        </div>
        <div className="postdb-modal-layout">
          <div className="postdb-modal-cover">
            {post.image_url ? <img src={mediaUrl(post.image_url)} alt={post.title} /> : null}
          </div>
          <div className="postdb-modal-content">
            <PostDbMetadataEditor
              post={post}
              metadataOptions={metadataOptions}
              onSave={(form) => onUpdatePost(post, form)}
              defaultOpen
            />
            <PostDbContentPanel post={post} />
            {post.analysis_summary ? <BrainSummary post={post} /> : <PendingCopy post={post} />}
          </div>
        </div>
      </section>
    </div>,
    document.body
  );
}

function postMatchesPostDbFilter(post: Post, filterKey: PostDbFilterKey) {
  switch (filterKey) {
    case "analyzed":
      return Boolean(post.analysis_summary || post.has_analysis_summary);
    case "needs_analysis":
      return !post.analysis_summary && !post.has_analysis_summary;
    case "has_hook":
      return hasHookText(post);
    case "carousel":
    case "image":
    case "video":
      return String(post.post_type_label ?? "").toLowerCase().includes(filterKey);
    case "all":
    default:
      return true;
  }
}

function comparePostDbPosts(left: Post, right: Post, sortKey: PostDbSortKey) {
  if (sortKey === "oldest") {
    return dateValue(left.published_at || left.created_at) - dateValue(right.published_at || right.created_at);
  }
  if (sortKey === "newest") {
    return dateValue(right.published_at || right.created_at) - dateValue(left.published_at || left.created_at);
  }
  const fields: Record<Exclude<PostDbSortKey, "newest" | "oldest">, keyof Post> = {
    likes: "likes",
    comments: "comments",
    brain_mean: "brain_global_mean_abs",
    brain_peak: "brain_global_peak_abs",
    virality: "virality_potential"
  };
  return numericPostValue(right, fields[sortKey]) - numericPostValue(left, fields[sortKey]);
}

function numericPostValue(post: Post, key: keyof Post) {
  const value = post[key];
  return typeof value === "number" && Number.isFinite(value) ? value : Number.NEGATIVE_INFINITY;
}

function dateValue(value?: string | null) {
  if (!value) return 0;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

function PostTags({ post }: { post: Post }) {
  const baseTags = [
    post.source_row_number ? { label: "Post DB #", value: String(post.source_row_number).padStart(4, "0") } : null,
    post.shortcode ? { label: "Shortcode", value: post.shortcode } : null,
    post.person_label ? { label: "Person", value: post.person_label } : null,
    post.company_label ? { label: "Company", value: post.company_label } : null,
    post.post_type_label ? { label: "Type", value: post.post_type_label } : null,
    post.is_animated ? { label: "Animated", value: "Yes" } : null,
    post.comments ? { label: "Comments", value: post.comments.toLocaleString() } : null,
    post.hook_text ? { label: "Hook", value: post.hook_text } : null,
  ].filter(Boolean) as Array<{ label: string; value: string }>;

  const customTags = (post.tags || []).map(tag => ({ label: "Tag", value: tag }));
  const tags = [...baseTags, ...customTags];

  if (!tags.length) return null;
  return (
    <div className="metadata-tags" aria-label="Post metadata">
      {tags.map((tag, i) => (
        <span className="metadata-chip" key={`${tag.label}-${tag.value}-${i}`}>
          <small>{tag.label}</small>
          {tag.value}
        </span>
      ))}
    </div>
  );
}

function PostDbContentPanel({ post }: { post: Post }) {
  return (
    <section className="postdb-content-panel" aria-label="Post DB content metadata">
      <div className="postdb-content-stats">
        <Metric label="Likes" value={String(post.likes ?? FLOP_LIKES_BASELINE)} />
        <Metric label="Comments" value={post.comments == null ? "N/A" : post.comments.toLocaleString()} />
      </div>
      <div className="postdb-content-grid">
        <div>
          <span>Caption</span>
          <p>{post.caption?.trim() || "No caption saved."}</p>
        </div>
        <div>
          <span>Modal OCR hook</span>
          <p>{post.hook_text?.trim() || "No text detected yet."}</p>
        </div>
      </div>
    </section>
  );
}

function BrainSummary({ post }: { post: Post }) {
  const summary = post.analysis_summary;
  if (!summary) return null;
  const metrics = summary.metrics;
  const prediction = post.calibrated_prediction;
  const isPostDb = post.section === "historical";

  return (
    <div className="brain-summary">
      <ViralityGauge
        score={summary.virality_potential ?? computeViralityPotential(summary)}
        mode={isPostDb ? "postDb" : "prediction"}
      />

      <div className="metric-grid">
        <Metric label="Mean activation" value={formatMetric(metrics.global_mean_abs)} />
        <Metric label="Global peak" value={formatMetric(metrics.global_peak_abs)} />
        <Metric label={isPostDb ? "Post DB percentile" : "Reference percentile"} value={post.tribe_percentile ? `P${post.tribe_percentile}` : "N/A"} />
        <Metric label="Segments" value={String(metrics.n_segments ?? 0)} />
      </div>

      {isPostDb ? (
        <PostDbLikesPanel post={post} />
      ) : prediction ? (
        <div className="prediction-row">
          <div className="prediction-box">
            <Trophy size={18} />
            <span>{prediction.predicted_likes.toLocaleString()} predicted likes</span>
            <small>
              {prediction.prediction_low != null && prediction.prediction_high != null
                ? `${prediction.prediction_low.toLocaleString()}-${prediction.prediction_high.toLocaleString()} range`
                : `${Math.round(prediction.confidence * 100)}% conf.`}
            </small>
          </div>
        </div>
      ) : (
        <div className="prediction-box muted">
          <BarChart3 size={18} />
          <span>Add Post DB posts with real likes to calibrate performance</span>
        </div>
      )}

      <div className="brain-and-networks">
        {isPostDb ? null : <BrainActivationView summary={summary} />}
        <NetworkBars networks={summary.networks} />
      </div>
      {summary.temporal_series.length > 2 ? <TemporalChart points={summary.temporal_series} /> : null}
      {isPostDb ? <PostDbBrainDataPanel post={post} /> : <InterpretationPanel post={post} />}
      {isPostDb ? null : <LlmReportPanel post={post} />}
      {summary.warnings.length ? <div className="inline-warning">{summary.warnings[0]}</div> : null}
    </div>
  );
}

function PostDbLikesPanel({ post }: { post: Post }) {
  if (!isFlopOutcome(post)) {
    return (
      <div className="postdb-likes-card real-likes">
        <BarChart3 size={18} />
        <div>
          <span>{post.likes?.toLocaleString()} real likes</span>
          <small>Used as ground truth for pattern finding.</small>
        </div>
      </div>
    );
  }

  return (
    <div className="postdb-likes-card missing-likes">
      <BarChart3 size={18} />
      <div>
        <span>Flop baseline: {(post.likes ?? FLOP_LIKES_BASELINE).toLocaleString()} likes</span>
        <small>Blank likes and 3 are trained as real low-performing outcomes.</small>
      </div>
    </div>
  );
}

function PostDbBrainDataPanel({ post }: { post: Post }) {
  const summary = post.analysis_summary;
  if (!summary) return null;
  const metricRows = Object.entries(summary.metrics).sort(([left], [right]) => left.localeCompare(right));
  const networkRows = Object.entries(summary.networks)
    .map(([key, network]) => ({ key, ...network }))
    .sort((left, right) => right.score - left.score);

  return (
    <section className="postdb-data-panel" aria-label="Post DB brain data">
      <div className="postdb-data-header">
        <Brain size={16} />
        <div>
          <h4>Brain report data</h4>
          <p>Structured TRIBE v2 outputs paired with real likes for pattern search.</p>
        </div>
      </div>

      <div className="postdb-meta-grid">
        <Metric label="Model" value={summary.model || "N/A"} />
        <Metric label="Mesh" value={summary.mesh || "N/A"} />
        <Metric label="ROI method" value={summary.roi_method || "N/A"} />
        <Metric label="Created" value={formatDateTime(summary.created_at)} />
      </div>

      <div className="postdb-table-grid">
        <DataTable
          title="Metrics"
          columns={["Metric", "Value"]}
          rows={metricRows.map(([key, value]) => [labelFromKey(key), formatMetric(value)])}
        />
        <DataTable
          title="Networks"
          columns={["Network", "Raw", "Score"]}
          rows={networkRows.map((network) => [
            networkLabels[network.key] ?? network.label,
            formatMetric(network.raw),
            formatMetric(network.score)
          ])}
        />
        <DataTable
          title="Top regions"
          columns={["Region", "Raw", "Score"]}
          rows={summary.top_regions.map((region) => [
            region.name,
            formatMetric(region.raw),
            formatMetric(region.score)
          ])}
        />
        <DataTable
          title="Temporal segments"
          columns={["Segment", "Start", "Mean", "Peak"]}
          rows={summary.temporal_series.map((point) => [
            String(point.index + 1),
            `${point.start.toFixed(1)}s`,
            formatMetric(point.mean_abs),
            formatMetric(point.peak_abs)
          ])}
        />
      </div>

      {summary.surface ? (
        <div className="surface-meta">
          <span>Surface vertices: {summary.surface.n_vertices.toLocaleString()}</span>
          <span>Samples: {summary.surface.values.length.toLocaleString()}</span>
          <span>Max: {formatMetric(summary.surface.max)}</span>
        </div>
      ) : null}
    </section>
  );
}

function DataTable({
  title,
  columns,
  rows
}: {
  title: string;
  columns: string[];
  rows: string[][];
}) {
  return (
    <div className="postdb-data-table">
      <h5>{title}</h5>
      <div className="data-table-scroll">
        <table>
          <thead>
            <tr>
              {columns.map((column) => <th key={column}>{column}</th>)}
            </tr>
          </thead>
          <tbody>
            {rows.length ? rows.map((row, rowIndex) => (
              <tr key={`${title}-${rowIndex}`}>
                {row.map((cell, cellIndex) => <td key={`${title}-${rowIndex}-${cellIndex}`}>{cell}</td>)}
              </tr>
            )) : (
              <tr>
                <td colSpan={columns.length}>No data</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function BrainActivationView({ summary }: { summary: NonNullable<Post["analysis_summary"]> }) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const [isVisible, setIsVisible] = useState(false);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    let timeout: number;
    const observer = new IntersectionObserver(([entry]) => {
      if (entry.isIntersecting) {
        timeout = window.setTimeout(() => setIsVisible(true), 150);
      } else {
        window.clearTimeout(timeout);
        setIsVisible(false);
      }
    }, { rootMargin: "0px" });
    observer.observe(canvas);
    return () => {
      window.clearTimeout(timeout);
      observer.disconnect();
    };
  }, []);

  useEffect(() => {
    const canvas = canvasRef.current;
    const surface = summary.surface;
    if (!canvas || !surface || !surface.values.length || !isVisible) return;
    if (activeBrainRenderers >= MAX_ACTIVE_BRAIN_RENDERERS) return;

    let renderer: THREE.WebGLRenderer;
    try {
      renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
    } catch (err) {
      return;
    }
    activeBrainRenderers += 1;
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
      activeBrainRenderers = Math.max(0, activeBrainRenderers - 1);
      window.cancelAnimationFrame(frame);
      window.removeEventListener("resize", resize);
      canvas.removeEventListener("pointerdown", onPointerDown);
      window.removeEventListener("pointermove", onPointerMove);
      window.removeEventListener("pointerup", onPointerUp);
      renderer.forceContextLoss();
      renderer.dispose();
      leftShell.geometry.dispose();
      rightShell.geometry.dispose();
      leftActivation.geometry.dispose();
      rightActivation.geometry.dispose();
      shellMaterial.dispose();
      activationMaterial.dispose();
    };
  }, [summary, isVisible]);

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

function ViralityGauge({
  score,
  mode = "prediction"
}: {
  score: number;
  mode?: "prediction" | "postDb";
}) {
  const pct = Math.round(score * 100);
  const color = score >= 0.70 ? "#d8ff3d"
    : score >= 0.50 ? "#a6ff00"
    : score >= 0.30 ? "#1cff93"
    : "#4a8c6a";
  const label = mode === "postDb"
    ? score >= 0.75 ? "Strong signal"
      : score >= 0.50 ? "Moderate signal"
      : score >= 0.30 ? "Light signal"
      : "Minimal signal"
    : score >= 0.75 ? "High potential"
      : score >= 0.50 ? "Moderate potential"
      : score >= 0.30 ? "Low potential"
      : "Minimal signal";
  const caption = mode === "postDb"
    ? "Composite signal · Social · Reward · Attention"
    : "Viral potential · Social · Reward · Attention";

  return (
    <div className="virality-gauge">
      <div className="gauge-score" style={{ color }}>
        {pct}<span>%</span>
      </div>
      <div className="gauge-body">
        <div className="gauge-labels">
          <strong style={{ color }}>{label}</strong>
          <small>{caption}</small>
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
          <h2>Post DB calibration</h2>
          <p>{calibration?.message || "Loading calibration..."}</p>
        </div>
      </div>
      <div className="metric-grid">
        <Metric label="Training samples" value={String(calibration?.sample_count ?? 0)} />
        <Metric label="Status" value={calibration?.ready ? "Ready" : "Pending"} />
        <Metric
          label="Holdout R2"
          value={calibration?.r2_validation == null ? "N/A" : calibration.r2_validation.toFixed(2)}
        />
        <Metric
          label="Holdout MAE"
          value={calibration?.mae_validation == null ? "N/A" : formatCompactNumber(calibration.mae_validation)}
        />
        <Metric
          label="Holdout error"
          value={calibration?.wape_validation == null ? "N/A" : `${Math.round(calibration.wape_validation * 100)}%`}
        />
        <Metric
          label="Median likes"
          value={calibration?.train_median_likes == null ? "N/A" : formatCompactNumber(calibration.train_median_likes)}
        />
      </div>
    </section>
  );
}

function PostDbOcrPanel({
  posts,
  health,
  loading,
  onRun
}: {
  posts: Post[];
  health: Health | null;
  loading: boolean;
  onRun: () => void;
}) {
  const ready = posts.filter((post) =>
    post.status === "completed" &&
    (post.analysis_summary || post.has_analysis_summary) &&
    !post.hook_text?.trim()
  ).length;
  const configured = Boolean(health?.remote_ocr?.configured);
  const canRun = configured && ready >= 100 && !loading;

  return (
    <section className="panel calibration-panel">
      <div className="panel-title">
        <FileText size={20} />
        <div>
          <h2>Modal OCR batch</h2>
          <p>{configured ? "Lower-half hook OCR" : "Configure REMOTE_OCR_URL"}</p>
        </div>
      </div>
      <div className="metric-grid">
        <Metric label="Ready" value={`${ready}/100`} />
        <Metric label="Crop" value="Lower half" />
        <Metric label="Mode" value="Modal only" />
      </div>
      <button className="primary-button" disabled={!canRun} onClick={onRun}>
        {loading ? <Loader2 className="spin" size={18} /> : <FileText size={18} />}
        Run 100-cover OCR
      </button>
    </section>
  );
}

function PostDbBatchUploadPanel({
  onSubmit,
  loading,
  metadataOptions
}: {
  onSubmit: (form: FormData) => Promise<void>;
  loading: boolean;
  metadataOptions: MetadataOptions;
}) {
  const inputId = useId();
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const [rows, setRows] = useState<PostDbBatchRow[]>([]);
  const [durationSeconds, setDurationSeconds] = useState("2");
  const [localError, setLocalError] = useState<string | null>(null);

  function handleFiles(event: ChangeEvent<HTMLInputElement>) {
    const files = Array.from(event.currentTarget.files ?? []);
    setLocalError(null);
    setRows(files.map((file) => ({
      id: typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID() : `${file.name}-${file.lastModified}`,
      file,
      title: titleFromFilename(file.name),
      publishedAt: "",
      likes: "",
      personLabel: "",
      companyLabel: "",
      postTypeLabel: "",
      caption: ""
    })));
  }

  function updateRow(id: string, patch: Partial<Omit<PostDbBatchRow, "id" | "file">>) {
    setRows((current) => current.map((row) => row.id === id ? { ...row, ...patch } : row));
  }

  function removeRow(id: string) {
    setRows((current) => current.filter((row) => row.id !== id));
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!rows.length) {
      setLocalError("Select at least one Post DB cover.");
      return;
    }

    const form = new FormData();
    form.set("section", "historical");
    form.set("analyze_now", "false");
    form.set("duration_seconds", durationSeconds);
    form.set("titles", JSON.stringify(rows.map((row) => row.title)));
    form.set("published_ats", JSON.stringify(rows.map((row) => row.publishedAt)));
    form.set("likes", JSON.stringify(rows.map((row) => row.likes)));
    form.set("person_labels", JSON.stringify(rows.map((row) => row.personLabel)));
    form.set("company_labels", JSON.stringify(rows.map((row) => row.companyLabel)));
    form.set("post_type_labels", JSON.stringify(rows.map((row) => row.postTypeLabel)));
    form.set("captions", JSON.stringify(rows.map((row) => row.caption)));
    rows.forEach((row) => form.append("files", row.file));

    await onSubmit(form);
    setRows([]);
    setLocalError(null);
    if (fileInputRef.current) {
      fileInputRef.current.value = "";
    }
  }

  return (
    <form className="panel upload-panel batch-upload-panel" onSubmit={handleSubmit}>
      <div className="panel-title">
        <Upload size={20} />
        <div>
          <h2>Batch Post DB import</h2>
          <p>Upload multiple published covers with dates and real likes. Analysis stays manual.</p>
        </div>
      </div>

      <label htmlFor={inputId}>
        Post DB covers
        <input
          id={inputId}
          ref={fileInputRef}
          className="visually-hidden-file"
          type="file"
          accept="image/png,image/jpeg,image/webp"
          multiple
          onChange={handleFiles}
        />
        <span className="file-picker">
          <ImagePlus size={18} />
          <strong>Select Post DB covers</strong>
          <small>{rows.length ? `${rows.length} selected` : "No covers selected"}</small>
        </span>
      </label>

      <label>
        Video duration
        <input
          type="number"
          min="2"
          max="10"
          value={durationSeconds}
          onChange={(event) => setDurationSeconds(event.currentTarget.value)}
        />
      </label>

      {rows.length ? (
        <div className="batch-list">
          {rows.map((row) => (
            <div className="batch-row" key={row.id}>
              <div className="batch-file">
                <strong>{row.file.name}</strong>
                <button
                  className="text-button danger-button compact-action"
                  type="button"
                  aria-label={`Remove ${row.file.name}`}
                  onClick={() => removeRow(row.id)}
                >
                  <Trash2 size={14} />
                  Remove
                </button>
              </div>
              <div className="batch-fields">
                <label>
                  Title
                  <input value={row.title} onChange={(event) => updateRow(row.id, { title: event.currentTarget.value })} />
                </label>
                <label>
                  Publish date
                  <input
                    type="date"
                    value={row.publishedAt}
                    onChange={(event) => updateRow(row.id, { publishedAt: event.currentTarget.value })}
                  />
                </label>
                <label>
                  Real likes
                  <input
                    type="number"
                    min="0"
                    value={row.likes}
                    placeholder="Optional"
                    onChange={(event) => updateRow(row.id, { likes: event.currentTarget.value })}
                  />
                  <small className="field-hint">Blank saves as {FLOP_LIKES_BASELINE} flop likes.</small>
                </label>
              </div>
              <MetadataInputs
                metadataOptions={metadataOptions}
                values={row}
                onChange={(patch) => updateRow(row.id, patch)}
              />
              <label className="batch-notes">
                Notes
                <textarea
                  rows={2}
                  value={row.caption}
                  placeholder="Post context or cover notes"
                  onChange={(event) => updateRow(row.id, { caption: event.currentTarget.value })}
                />
              </label>
            </div>
          ))}
        </div>
      ) : null}

      {localError ? <div className="inline-error">{localError}</div> : null}
      <button className="primary-button" disabled={loading || !rows.length}>
        {loading ? <Loader2 className="spin" size={18} /> : <Upload size={18} />}
        Import to Post DB
      </button>
    </form>
  );
}

function PostDbMetadataEditor({
  post,
  metadataOptions,
  onSave,
  defaultOpen = false
}: {
  post: Post;
  metadataOptions: MetadataOptions;
  onSave: (form: FormData) => Promise<void>;
  defaultOpen?: boolean;
}) {
  const [editing, setEditing] = useState(defaultOpen);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);

  useEffect(() => {
    setEditing(defaultOpen);
    setLocalError(null);
    setSaved(false);
  }, [post.id, defaultOpen]);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    if (!String(form.get("likes") ?? "").trim()) {
      form.set("likes", String(FLOP_LIKES_BASELINE));
    }
    setSaving(true);
    setLocalError(null);
    setSaved(false);
    try {
      await onSave(form);
      setSaved(true);
      if (!defaultOpen) setEditing(false);
      window.setTimeout(() => setSaved(false), 2500);
    } catch (caught) {
      setLocalError(caught instanceof Error ? caught.message : "Could not update Post DB metadata.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className="postdb-editor">
      <div className="postdb-editor-header">
        <div>
          <h4>Post details</h4>
          <p>{post.likes == null ? "No likes saved; this trains as a flop baseline." : likesOutcomeText(post)}</p>
        </div>
        {!defaultOpen ? (
          <button className="text-button compact-action" type="button" onClick={() => setEditing((current) => !current)}>
            <Pencil size={15} />
            {editing ? "Close" : "Edit"}
          </button>
        ) : null}
      </div>

      {editing ? (
        <form className="postdb-edit-form" onSubmit={handleSubmit}>
          <fieldset className="edit-fieldset" disabled={saving}>
            <legend>Basics</legend>
            <label>
              Title
              <input name="title" defaultValue={post.title} required />
            </label>
            <div className="form-row">
              <label>
                Publish date
                <input name="published_at" type="date" defaultValue={dateInputValue(post.published_at)} />
              </label>
              <label className="checkbox-field checkbox-field-inline">
                <span>Animated original</span>
                <input name="is_animated" type="checkbox" defaultChecked={Boolean(post.is_animated)} />
              </label>
            </div>
          </fieldset>

          <fieldset className="edit-fieldset" disabled={saving}>
            <legend>Performance</legend>
            <div className="form-row">
              <label>
                Likes
                <input name="likes" type="number" min="0" defaultValue={String(post.likes ?? FLOP_LIKES_BASELINE)} />
                <small className="field-hint">Blank saves as {FLOP_LIKES_BASELINE} flop likes.</small>
              </label>
              <label>
                Comments
                <input name="comments" type="number" min="0" defaultValue={post.comments ?? ""} placeholder="Optional" />
              </label>
            </div>
          </fieldset>

          <fieldset className="edit-fieldset" disabled={saving}>
            <legend>Context</legend>
            <MetadataInputs
              metadataOptions={metadataOptions}
              values={{
                personLabel: post.person_label ?? "",
                companyLabel: post.company_label ?? "",
                postTypeLabel: post.post_type_label ?? ""
              }}
            />
            <TagsInput
              name="tags"
              label="Tags"
              placeholder="Comma-separated labels"
              options={metadataOptions.tags || []}
              value={(post.tags || []).join(", ")}
            />
            <label>
              Hook text
              <input name="hook_text" defaultValue={post.hook_text ?? ""} placeholder="Visible text on cover" />
            </label>
            <label>
              Caption / Notes
              <textarea name="caption" rows={3} defaultValue={post.caption ?? ""} />
            </label>
          </fieldset>

          {localError ? <div className="inline-error">{localError}</div> : null}
          <div className="postdb-editor-actions">
            {saved ? (
              <span className="save-confirmation">
                <CheckCircle2 size={15} />
                Saved
              </span>
            ) : null}
            {!defaultOpen ? (
              <button className="text-button" type="button" disabled={saving} onClick={() => setEditing(false)}>
                Cancel
              </button>
            ) : null}
            <button className="save-button" disabled={saving}>
              {saving ? <Loader2 className="spin" size={15} /> : <Save size={15} />}
              {saving ? "Saving..." : "Save changes"}
            </button>
          </div>
        </form>
      ) : null}
    </section>
  );
}

function LikesEditor({
  post,
  onSave,
  buttonLabel = "Update likes",
  helperText = `Blank saves as ${FLOP_LIKES_BASELINE} flop likes.`
}: {
  post: Post;
  onSave: (likes: number) => Promise<void>;
  buttonLabel?: string;
  helperText?: string;
}) {
  const [value, setValue] = useState(likesInputValue(post));
  const [saving, setSaving] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);

  useEffect(() => {
    setValue(likesInputValue(post));
    setLocalError(null);
  }, [post.id, post.likes]);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const likes = value.trim() === "" ? FLOP_LIKES_BASELINE : Number(value);
    if (!Number.isFinite(likes) || likes < 0) {
      setLocalError("Enter a valid like count.");
      return;
    }
    setSaving(true);
    setLocalError(null);
    try {
      await onSave(Math.round(likes));
    } catch (caught) {
      setLocalError(caught instanceof Error ? caught.message : "Could not update likes.");
    } finally {
      setSaving(false);
    }
  }

  const unchanged = value === likesInputValue(post);

  return (
    <form className="likes-editor" onSubmit={handleSubmit}>
      <label>
        Real likes
        <input
          type="number"
          min="0"
          value={value}
          placeholder="Add likes"
          onChange={(event) => setValue(event.currentTarget.value)}
        />
        <small className="field-hint">{helperText}</small>
      </label>
      <button className="text-button compact-action" disabled={saving || unchanged}>
        {saving ? <Loader2 className="spin" size={15} /> : <CheckCircle2 size={15} />}
        {buttonLabel}
      </button>
      {localError ? <div className="inline-error">{localError}</div> : null}
    </form>
  );
}

function AbUploadPanel({
  onSubmit,
  loading,
  metadataOptions
}: {
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
  loading: boolean;
  metadataOptions: MetadataOptions;
}) {
  return (
    <form className="panel upload-panel ab-upload-panel form-workflow" onSubmit={onSubmit}>
      <div className="panel-title">
        <FlaskConical size={20} />
        <div>
          <h2>Compare covers</h2>
          <p>Upload two candidates. Rankings use calibrated likes when enough Post DB data exists.</p>
        </div>
      </div>
      <section className="analyze-section ab-section">
        <FormStepHeader
          step="01"
          title="Test setup"
          description="Give this comparison a clear name and choose the generated video length."
        />
        <div className="form-row">
          <label>
            Test name
            <input name="name" placeholder="Pope AI carousel cover test" required />
            <small className="field-hint">This becomes the saved test name in the list below.</small>
          </label>
          <label>
            Video duration
            <input name="duration_seconds" type="number" min="2" max="10" defaultValue="2" />
            <small className="field-hint">Use 2 seconds for static covers.</small>
          </label>
        </div>
      </section>

      <section className="analyze-section ab-section">
        <FormStepHeader
          step="02"
          title="Candidates"
          description="Add the two cover options you want ranked. Each one becomes its own analysis job."
        />
        <div className="ab-candidate-grid">
          <FileInput
            label="Cover A"
            name="files"
            hint="First candidate image."
          />
          <FileInput
            label="Cover B"
            name="files"
            hint="Second candidate image."
          />
        </div>
      </section>

      <section className="analyze-section ab-section">
        <FormStepHeader
          step="03"
          title="Shared metadata"
          description="Apply common labels to every candidate so results stay organized."
        />
        <MetadataInputs metadataOptions={metadataOptions} showSuggestions={false} compact />
      </section>

      <div className="ab-form-notes">
        <span><Target size={15} />Ranks by calibrated likes when available.</span>
        <span><ListChecks size={15} />Keeps every candidate editable in results.</span>
      </div>

      <FormSubmitBar
        loading={loading}
        label="Start comparison"
        helper="Creates one analysis job per candidate and auto-selects a winner when all jobs finish."
      />
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
                <PostTags post={candidate} />
              </div>
              <div className="result-actions">
                {candidate.is_winner ? <span className="winner-pill"><Trophy size={14} />Chosen winner</span> : null}
                <StatusPill post={candidate} />
                <DeleteAction
                  ariaLabel={`Delete ${candidate.title}`}
                  confirmText="Delete this candidate result?"
                  onConfirm={() => onDeleteCandidate(candidate)}
                />
              </div>
            </div>
            <ProgressIndicator post={candidate} />
            {candidate.analysis_summary ? <BrainSummary post={candidate} /> : <PendingCopy post={candidate} />}
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

function StatusPill({ post }: { post: Post }) {
  if (isIdlePostDbPost(post)) {
    return <span className="status-pill idle">Stored</span>;
  }

  const status = post.status;
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

function InstagramLinkPanel({
  onSubmit,
  loading
}: {
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
  loading: boolean;
}) {
  return (
    <form className="panel upload-panel instagram-link-panel form-workflow" onSubmit={onSubmit}>
      <div className="panel-title">
        <Cloud size={20} />
        <div>
          <h2>Instagram link</h2>
          <p>Fast path for published posts. Paste the normal post link; the app imports caption and the first cover image.</p>
        </div>
      </div>
      <section className="analyze-section import-section">
        <FormStepHeader
          step="IG"
          title="Import from post"
          description="No cover URL needed. Use any normal /p/, /reel/, or /tv/ Instagram URL."
        />
        <label>
          Post URL
          <input
            name="instagram_url"
            type="url"
            placeholder="https://www.instagram.com/p/..."
            required
          />
        </label>
        <div className="form-row">
          <label>
            Video duration
            <input name="duration_seconds" type="number" min="2" max="10" defaultValue="2" />
          </label>
          <label className="checkbox-field checkbox-field-inline">
            <span>Analyze now</span>
            <input name="analyze_now" type="checkbox" defaultChecked />
          </label>
        </div>
      </section>
      <FormSubmitBar
        loading={loading}
        label="Import Instagram post"
        helper="Imports caption and cover, then starts analysis when Analyze now is checked."
      />
    </form>
  );
}

function FileInput({
  label,
  name,
  multiple = false,
  hint
}: {
  label: string;
  name: string;
  multiple?: boolean;
  hint?: string;
}) {
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
      <span className={multiple ? "file-picker multi-file-picker" : "file-picker"}>
        <ImagePlus size={18} />
        <strong>{multiple ? "Select covers" : "Select cover"}</strong>
        <small>{fileNames.length ? fileNames.join(", ") : multiple ? "No candidates selected" : "No image selected"}</small>
      </span>
      {hint ? <small className="field-hint">{hint}</small> : null}
    </label>
  );
}

function PendingCopy({ post }: { post: Post }) {
  if (isIdlePostDbPost(post)) {
    return (
      <div className="pending-copy">
        <Database size={18} />
        Stored in Post DB. Run analysis manually when you are ready to generate the brain data.
      </div>
    );
  }

  if (post.status === "failed") return null;
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

function isActivelyAnalyzing(post: Post) {
  return post.status === "running" || (post.status === "queued" && (post.progress_percent ?? 0) > 0);
}

function isIdlePostDbPost(post: Post) {
  return post.section === "historical" && !post.analysis_summary && !post.error && !isActivelyAnalyzing(post);
}

function canAnalyzePost(post: Post) {
  if (post.status === "running" || isActivelyAnalyzing(post)) return false;
  if (isIdlePostDbPost(post)) return true;
  return post.status === "failed" || post.status === "completed";
}

function isFlopOutcome(post: Pick<Post, "likes">) {
  return post.likes == null || post.likes <= FLOP_LIKES_BASELINE;
}

function likesInputValue(post: Pick<Post, "likes">) {
  return post.likes == null ? "" : String(post.likes);
}

function likesOutcomeText(post: Pick<Post, "likes">) {
  return isFlopOutcome(post)
    ? `${post.likes ?? FLOP_LIKES_BASELINE} likes · flop outcome`
    : `${post.likes?.toLocaleString()} likes`;
}

function dateInputValue(value?: string | null) {
  return value ? value.slice(0, 10) : "";
}

function normalizePostForm(form: FormData) {
  for (const field of ["tags", "person_label", "company_label"]) {
    const value = form.get(field);
    if (typeof value !== "string") continue;
    const values = value.split(",").map((item) => item.trim()).filter(Boolean);
    form.set(field, JSON.stringify(values));
  }
  if (form.has("likes") && !String(form.get("likes") ?? "").trim()) {
    form.set("likes", String(FLOP_LIKES_BASELINE));
  }
  if (form.has("comments") && !String(form.get("comments") ?? "").trim()) {
    form.delete("comments");
  }
  const isAnimated = form.get("is_animated") === "on" || form.get("is_animated") === "true";
  form.set("is_animated", isAnimated ? "true" : "false");
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
    medianTopLikes: median(topLikes),
    avgTopComments: average(topPosts.map(p => p.comments ?? 0)),
    medianTopComments: median(topPosts.map(p => p.comments ?? 0))
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
  const groups = new Map<string, { count: number; likes: number; maxLikes: number; hooks: number; comments: number; maxComments: number }>();
  for (const post of posts) {
    const labels = labelsForPost(post);
    for (const label of labels) {
      const current = groups.get(label) ?? { count: 0, likes: 0, maxLikes: 0, hooks: 0, comments: 0, maxComments: 0 };
      const likes = post.likes ?? 0;
      const comments = post.comments ?? 0;
      current.count += 1;
      current.likes += likes;
      current.maxLikes = Math.max(current.maxLikes, likes);
      current.comments += comments;
      current.maxComments = Math.max(current.maxComments, comments);
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
      avgComments: value.comments / Math.max(1, value.count),
      maxComments: value.maxComments,
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

function labelFromKey(key: string) {
  return key
    .split("_")
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function formatDateTime(value?: string) {
  if (!value) return "N/A";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
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
    : "This is a neural response profile, not a like prediction yet. Add Post DB posts with real likes to calibrate it.";

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
      label: "Post DB context",
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
        body: "This ranks high against your analyzed Post DB."
      };
    }
    if (post.tribe_percentile <= 35) {
      return {
        value: `P${post.tribe_percentile}`,
        body: "This ranks low against your current Post DB."
      };
    }
    return {
      value: `P${post.tribe_percentile}`,
      body: "This sits around the middle of your current Post DB."
    };
  }
  return {
    value: "Not calibrated",
    body: "There are not enough Post DB posts with real likes to compare this score against your own audience."
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

function titleFromFilename(filename: string) {
  return filename
    .replace(/\.[^.]+$/, "")
    .replace(/[-_]+/g, " ")
    .trim()
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function AccessGate() {
  const [state, setState] = useState<"checking" | "required" | "granted">("checking");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [gateError, setGateError] = useState<string | null>(null);
  const [verifying, setVerifying] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const result = await checkAuth(getApiKey());
        if (cancelled) return;
        setState(!result.auth_required || result.ok ? "granted" : "required");
      } catch {
        // Backend unreachable: let the app render and show its own error state.
        if (!cancelled) setState("granted");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!username.trim() || !password) return;
    setVerifying(true);
    setGateError(null);
    try {
      const body = new FormData();
      body.set("username", username.trim());
      body.set("password", password);
      const response = await fetch(`${API_BASE}/api/auth/login`, { method: "POST", body });
      if (response.status === 401) {
        setGateError("Usuario o contraseña incorrectos.");
        return;
      }
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = (await response.json()) as { ok: boolean; token: string };
      if (data.ok) {
        if (data.token) setApiKey(data.token);
        setState("granted");
      } else {
        setGateError("Usuario o contraseña incorrectos.");
      }
    } catch {
      setGateError("No se pudo conectar con el servidor. Intenta de nuevo.");
    } finally {
      setVerifying(false);
    }
  }

  if (state === "granted") return <CortexRunApp />;
  if (state === "checking") {
    return (
      <div className="access-gate">
        <div className="access-card">
          <p className="product-name">Sentient</p>
          <h1>Cortex</h1>
          <p className="access-copy">Checking access...</p>
        </div>
      </div>
    );
  }
  return (
    <div className="access-gate">
      <form className="access-card" onSubmit={handleSubmit}>
        <p className="product-name">Sentient</p>
        <h1>Cortex</h1>
        <p className="access-copy">Inicia sesión para continuar.</p>
        <label>
          Usuario
          <input
            type="text"
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            placeholder="Usuario"
            autoComplete="username"
            autoFocus
          />
        </label>
        <label>
          Contraseña
          <input
            type="password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            placeholder="Contraseña"
            autoComplete="current-password"
          />
        </label>
        {gateError ? <div className="inline-error">{gateError}</div> : null}
        <button className="primary-button" disabled={verifying || !username.trim() || !password}>
          {verifying ? <Loader2 className="spin" size={16} /> : <Lock size={16} />}
          {verifying ? "Verificando..." : "Entrar"}
        </button>
      </form>
    </div>
  );
}

async function checkAuth(key: string | null): Promise<{ auth_required: boolean; ok: boolean }> {
  const headers: Record<string, string> = key ? { "X-API-Key": key } : {};
  const response = await fetch(`${API_BASE}/api/auth/check`, { headers });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

export default AccessGate;
