/**
 * 大西瓜 - 前端主逻辑
 * v7.9.9 私人陪伴系统：长前缀缓存链 + 听海本机耳朵 + 阅读模式统一层
 */
(function () {
  'use strict';

  // Safari private browsing, embedded WebViews and storage pressure can make
  // the localStorage getter or individual writes throw. Keep the app usable
  // with a page-lifetime fallback instead of aborting all initialization.
  const volatileStorage = new Map();
  let browserStorage = null;
  try { browserStorage = window.localStorage; } catch (_) { /* memory fallback */ }
  const localStorage = Object.freeze({
    getItem(key) {
      const normalized = String(key);
      try {
        const value = browserStorage?.getItem(normalized);
        return value === null || value === undefined
          ? (volatileStorage.get(normalized) ?? null)
          : value;
      } catch (_) {
        return volatileStorage.get(normalized) ?? null;
      }
    },
    setItem(key, value) {
      const normalized = String(key);
      const stored = String(value);
      volatileStorage.set(normalized, stored);
      try { browserStorage?.setItem(normalized, stored); } catch (_) { /* memory fallback */ }
    },
    removeItem(key) {
      const normalized = String(key);
      volatileStorage.delete(normalized);
      try { browserStorage?.removeItem(normalized); } catch (_) { /* memory fallback */ }
    },
  });
  window.DaxiguaStorage = localStorage;

  const companionName = document.documentElement.dataset.companionName || '小机';
  const companionInitial = document.documentElement.dataset.companionInitial
    || companionName.slice(-1)
    || '机';
  const frontendRevision = document.documentElement.dataset.assetRevision
    || document.documentElement.dataset.appVersion
    || '8.6';
  const LEGACY_THEME_KEYS = Object.freeze([
    'daxigua:v651-theme',
    'daxigua:v65-theme',
    'daxigua:v64-theme',
    'daxigua:v63-theme',
  ]);

  // ━━ 状态 ━━
  const state = {
    currentSession: null,
    sessions: [],
    sessionPageSize: 200,
    sessionHasMore: false,
    messagePageSize: 80,
    messageDomLimit: 240,
    oldestMessageId: 0,
    latestMessageId: 0,
    totalMessageCount: 0,
    historyStartPosition: 0,
    historyEndPosition: 0,
    hasOlderMessages: false,
    loadingOlderMessages: false,
    historyWindowAtLatest: true,
    loadedSessionId: null,
    historyNavigationEpoch: 0,
    historyRenderToken: 0,
    historyPageController: null,
    isStreaming: false,
    activeChatController: null,
    activeRequestId: null,
    activeClientRequestId: null,
    activeUserMessageEl: null,
    stopRequested: false,
    recoveredRequest: null,
    recoveringPendingRequest: false,
    reliabilityPollTimer: null,
    recoveryDraftRequestId: null,
    currentView: 'home',
    viewTrail: ['home'],
    viewTransitionTimer: null,
    providers: {},
    keyCredentials: {},
    activeProvider: 'deepseek',
    activeModel: '',
    models: [],
    modelPayload: null,
    activeBrainCapabilities: null,
    brainOptions: {},
    pendingAttachments: [],
    uploadingAttachments: 0,
    workspaceFiles: [],
    workspaceStats: { files: 0, bytes: 0, active: 0 },
    workspaceModes: { off: 0, retrieval: 0, pinned: 0 },
    ocean: null,
    oceanPollTimer: null,
    lastContextBudget: null,
    lastContextUsage: null,
    contextCompression: null,
    conversationImportFile: null,
    conversationImportBatchId: null,
    conversationImportPollTimer: null,
    conversationImportUploadRequest: null,
    conversationImportStatus: '',
    stickers: [],
    stickerMode: localStorage.getItem('companion:sticker-mode') || 'off',
    voice: null,
    activeVoiceAudio: null,
    voiceCapture: null,
    voiceCallId: null,
    voiceCallActive: false,
    voiceRoute: 'speaker',
    relationship: null,
    continuityDialogMode: 'thread',
    innerState: null,
    intimacyVitals: null,
    intimacyVitalsPollTimer: null,
    activeInnerDomain: 'emotion',
    coPresence: null,
    naturalMessagePollTimer: null,
    apiUsagePeriod: '7d',
  };

  // 只在当前页面内短暂保存计时与数量。这里不会保存草稿字符串；
  // 发往服务端的 payload 也只有时长、次数、输入方式和“是否仍有草稿”。
  const presence = {
    composing: false,
    active: false,
    startedAt: 0,
    lastInputAt: 0,
    lastLength: 0,
    lastSignalAt: 0,
    revisions: 0,
    pauses: 0,
    clears: 0,
    pastes: 0,
    inputEvents: 0,
    deletions: 0,
    bursts: 0,
    longestPauseMs: 0,
    inputMethod: 'keyboard',
    pauseTimer: null,
    heartbeatTimer: null,
  };

  // Draft strings remain browser-local and are keyed by session.  They are never
  // sent to the presence API; switching windows cannot reassign A's draft to B.
  const sessionDrafts = new Map();

  // ━━ DOM ━━
  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => document.querySelectorAll(sel);

  const dom = {
    sidebar: $('#sidebar'),
    sessionList: $('#session-list'),
    sessionSearch: $('#session-search'),
    btnSessionMore: $('#btn-session-more'),
    messages: $('#messages'),
    input: $('#input'),
    btnSend: $('#btn-send'),
    btnAttach: $('#btn-attach'),
    attachmentInput: $('#attachment-input'),
    attachmentTray: $('#attachment-tray'),
    attachmentHint: $('#attachment-hint'),
    contextBudgetPill: $('#context-budget-pill'),
    contextInspectorDialog: $('#context-inspector-dialog'),
    contextInspectorClose: $('#context-inspector-close'),
    contextInspectorBudget: $('#context-inspector-budget'),
    contextCompressionEnabled: $('#context-compression-enabled'),
    btnContextCompact: $('#btn-context-compact'),
    btnContextRebuild: $('#btn-context-rebuild'),
    contextCompressionStats: $('#context-compression-stats'),
    contextChapterList: $('#context-chapter-list'),
    contextSourcePanel: $('#context-source-panel'),
    contextStackList: $('#context-stack-list'),
    contextFileList: $('#context-file-list'),
    contextActualUsage: $('#context-actual-usage'),
    workspaceContextStrip: $('#workspace-context-strip'),
    workspaceContextLabel: $('#workspace-context-label'),
    btnWorkspaceManage: $('#btn-workspace-manage'),
    btnFileWorkspace: $('#btn-file-workspace'),
    btnVoiceMessage: $('#btn-voice-message'),
    btnVoiceCall: $('#btn-voice-call'),
    voiceCaptureStatus: $('#voice-capture-status'),
    workspaceDialog: $('#file-workspace-dialog'),
    workspaceDialogClose: $('#file-workspace-dialog-close'),
    workspaceDialogSearch: $('#workspace-dialog-search'),
    workspaceDialogList: $('#workspace-dialog-list'),
    btnWorkspaceDialogUpload: $('#btn-workspace-dialog-upload'),
    btnWorkspaceUpload: $('#btn-workspace-upload'),
    btnWorkspaceRefresh: $('#btn-workspace-refresh'),
    workspaceSearch: $('#workspace-search'),
    workspaceFileList: $('#workspace-file-list'),
    workspaceFileCount: $('#workspace-file-count'),
    workspaceRetrievalCount: $('#workspace-retrieval-count'),
    workspaceActiveCount: $('#workspace-active-count'),
    workspaceTotalSize: $('#workspace-total-size'),
    oceanReadyBadge: $('#ocean-ready-badge'),
    oceanPrivacy: $('#ocean-privacy'),
    oceanQueue: $('#ocean-queue'),
    oceanNote: $('#ocean-note'),
    oceanInstallLog: $('#ocean-install-log'),
    oceanLogDetails: $('#ocean-log-details'),
    btnOceanInstall: $('#btn-ocean-install'),
    btnOceanRefresh: $('#btn-ocean-refresh'),
    btnMenu: $('#btn-menu'),
    btnNewSession: $('#btn-new-session'),
    btnHome: $('#btn-home'),
    btnChatHome: $('#btn-chat-home'),
    btnUs: $('#btn-us'),
    btnMemory: $('#btn-memory'),
    btnConsole: $('#btn-console'),
    btnSystem: $('#btn-system'),
    btnWeatherClear: $('#btn-weather-clear'),
    btnFlowerSea: $('#btn-flower-sea'),
    naturalMemoryCount: $('#natural-memory-count'),
    naturalMemoryList: $('#natural-memory-list'),
    btnNaturalMemoryRefresh: $('#btn-natural-memory-refresh'),
    fontMode: $('#font-mode'),
    textScale: $('#text-scale'),
    consoleNav: $('#console-nav'),
    fontPreview: $('#font-preview'),
    headerTitle: $('#header-title'),
    headerStatus: $('#header-status'),
    btnModelHub: $('#btn-model-hub'),
    modelPillText: $('#model-pill-text'),
    healthDot: $('#health-dot'),
    modelSearch: $('#model-search'),
    customModelId: $('#custom-model-id'),
    btnApplyCustomModel: $('#btn-apply-custom-model'),
    btnRefreshModels: $('#btn-refresh-models'),
    keyCredential: $('#key-credential'),
    keyValue: $('#key-value'),
    keyManagerBadge: $('#key-manager-badge'),
    keyManagerStatus: $('#key-manager-status'),
    btnKeyToggle: $('#btn-key-toggle'),
    btnKeyClear: $('#btn-key-clear'),
    btnKeySave: $('#btn-key-save'),
    modelList: $('#model-list'),
    modelListMeta: $('#model-list-meta'),
    brainProtocol: $('#brain-protocol'),
    brainCapabilityBadges: $('#brain-capability-badges'),
    reasoningEffort: $('#reasoning-effort'),
    reasoningEffortWrap: $('#reasoning-effort-wrap'),
    thinkingMode: $('#thinking-mode'),
    thinkingModeWrap: $('#thinking-mode-wrap'),
    thinkingModeLabel: $('#thinking-mode-label'),
    reasoningContext: $('#reasoning-context'),
    reasoningContextWrap: $('#reasoning-context-wrap'),
    thinkingBudget: $('#thinking-budget'),
    thinkingBudgetWrap: $('#thinking-budget-wrap'),
    thinkingVisibility: $('#thinking-visibility'),
    thinkingVisibilityWrap: $('#thinking-visibility-wrap'),
    maxOutputTokens: $('#max-output-tokens'),
    brainSettingsNote: $('#brain-settings-note'),
    verbosity: $('#verbosity'),
    verbosityWrap: $('#verbosity-wrap'),
    btnBrainPreview: $('#btn-brain-preview'),
    brainPreviewOutput: $('#brain-preview-output'),
    viewChat: $('#view-chat'),
    contextMenu: $('#context-menu'),
    btnDiagnosticRefresh: $('#btn-diagnostic-refresh'),
    honestyEnabled: $('#honesty-enabled'),
    btnSelfTest: $('#btn-self-test'),
    btnClearErrors: $('#btn-clear-errors'),
    diagnosticHealth: $('#diagnostic-health'),
    diagnosticErrors: $('#diagnostic-errors'),
    diagnosticTraces: $('#diagnostic-traces'),
    diagnosticDetail: $('#diagnostic-detail'),
    stickerMode: $('#sticker-mode'),
    stickerCount: $('#sticker-count'),
    stickerList: $('#sticker-list'),
    stickerInput: $('#sticker-input'),
    btnImportSticker: $('#btn-import-sticker'),
    voiceReadyBadge: $('#voice-ready-badge'),
    voiceEnabled: $('#voice-enabled'),
    voiceAutoPlay: $('#voice-auto-play'),
    voiceTranscriptAutoSend: $('#voice-transcript-auto-send'),
    voiceCallAutoReply: $('#voice-call-auto-reply'),
    voiceId: $('#voice-id'),
    voiceOptions: $('#voice-options'),
    voiceModel: $('#voice-model'),
    voiceDelivery: $('#voice-delivery'),
    voiceStability: $('#voice-stability'),
    voiceStabilityValue: $('#voice-stability-value'),
    voiceSimilarity: $('#voice-similarity'),
    voiceSimilarityValue: $('#voice-similarity-value'),
    voiceStyle: $('#voice-style'),
    voiceStyleValue: $('#voice-style-value'),
    voiceSpeed: $('#voice-speed'),
    voiceSpeedValue: $('#voice-speed-value'),
    voiceSpeedWrap: $('#voice-speed-wrap'),
    voiceSpeedNote: $('#voice-speed-note'),
    voiceKeyterms: $('#voice-keyterms'),
    voiceGreetingText: $('#voice-greeting-text'),
    voiceTranslationEnabled: $('#voice-translation-enabled'),
    voiceMoodEnabled: $('#voice-mood-enabled'),
    voicePostEq: $('#voice-post-eq'),
    voiceTestText: $('#voice-test-text'),
    voicePreview: $('#voice-preview'),
    voiceSettingsNote: $('#voice-settings-note'),
    btnRefreshVoices: $('#btn-refresh-voices'),
    btnSaveVoice: $('#btn-save-voice'),
    btnTestVoice: $('#btn-test-voice'),
    btnGenerateGreeting: $('#btn-generate-greeting'),
    voiceCallDialog: $('#voice-call-dialog'),
    voiceCallStatus: $('#voice-call-status'),
    voiceCallSubtitles: $('#voice-call-subtitles'),
    voiceCallAudio: $('#voice-call-audio'),
    voiceCallTalk: $('#voice-call-talk'),
    voiceCallRoute: $('#voice-call-route'),
    voiceCallEnd: $('#voice-call-end'),
    voiceCallClose: $('#voice-call-close'),
    btnCoreRefresh: $('#btn-core-refresh'),
    btnCoreReset: $('#btn-core-reset'),
    coreIntimacyMode: $('#core-intimacy-mode'),
    coreSummary: $('#core-summary'),
    archiveCount: $('#archive-count'),
    coreIntention: $('#core-intention'),
    coreMeta: $('#core-meta'),
    coreBars: $('#core-bars'),
    coreEvents: $('#core-events'),
    corePlugins: $('#core-plugins'),
    coreIntentions: $('#core-intentions'),
    diagnosticExpanded: $('#diagnostic-expanded'),
    archiveQuery: $('#archive-query'),
    btnArchiveSearch: $('#btn-archive-search'),
    archiveResults: $('#archive-results'),
    btnCharacterSave: $('#btn-character-save'),
    characterEnabled: $('#character-enabled'),
    phraseFatigue: $('#phrase-fatigue'),
    punctuationFatigue: $('#punctuation-fatigue'),
    watchPhrases: $('#watch-phrases'),
    characterNativeSummary: $('#character-native-summary'),
    characterFatigued: $('#character-fatigued'),
    characterDashes: $('#character-dashes'),
    characterAudits: $('#character-audits'),
    btnCharacterLab: $('#btn-character-lab'),
    personaLabScore: $('#persona-lab-score'),
    personaLabContract: $('#persona-lab-contract'),
    personaLabMetrics: $('#persona-lab-metrics'),
    personaLabIssues: $('#persona-lab-issues'),
    btnLivingTick: $('#btn-living-tick'),
    btnLivingReset: $('#btn-living-reset'),
    btnEnablePush: $('#btn-enable-push'),
    btnTestInitiative: $('#btn-test-initiative'),
    btnTestMorningTrigger: $('#btn-test-morning-trigger'),
    livingEnabled: $('#living-enabled'),
    coPresenceNatural: $('#co-presence-natural'),
    coPresenceIndependent: $('#co-presence-independent'),
    coPresenceRhythm: $('#co-presence-rhythm'),
    livingDreams: $('#living-dreams'),
    livingMorning: $('#living-morning'),
    livingPhase: $('#living-phase'),
    livingTime: $('#living-time'),
    livingEvent: $('#living-event'),
    livingActivity: $('#living-activity'),
    livingContacts: $('#living-contacts'),
    pushStatus: $('#push-status'),
    coPresencePrivacy: $('#co-presence-privacy'),
    coPresenceRhythmSummary: $('#co-presence-rhythm-summary'),
    coPresenceRules: $('#co-presence-rules'),
    coPresenceTimeline: $('#co-presence-timeline'),
    livingBodyBars: $('#living-body-bars'),
    livingSocialBars: $('#living-social-bars'),
    livingTimeline: $('#living-timeline'),
    btnInnerRefresh: $('#btn-inner-refresh'),
    innerDomainSummary: $('#inner-domain-summary'),
    innerDomainBars: $('#inner-domain-bars'),
    innerSpecialEvent: $('#inner-special-event'),
    innerChangeFeed: $('#inner-change-feed'),
    innerVisibleChanges: $('#inner-visible-changes'),
    innerDetailMode: $('#inner-detail-mode'),
    innerTakeoverLevel: $('#inner-takeover-level'),
    innerTakeoverNote: $('#inner-takeover-note'),
    innerIntimacyPhase: $('#inner-intimacy-phase'),
    innerIntimacyNote: $('#inner-intimacy-note'),
    innerMorningLevel: $('#inner-morning-level'),
    innerMorningNote: $('#inner-morning-note'),
    innerOsSummary: $('#inner-os-summary'),
    innerOsNote: $('#inner-os-note'),
    innerEmotionTakeover: $('#inner-emotion-takeover'),
    innerIntimacyEnabled: $('#inner-intimacy-enabled'),
    innerIntimacyVitalsVisible: $('#inner-intimacy-vitals-visible'),
    innerMorningTakeover: $('#inner-morning-takeover'),
    innerMorningProactive: $('#inner-morning-proactive'),
    innerReflectionMode: $('#inner-reflection-mode'),
    innerIntimacyMode: $('#inner-intimacy-mode'),
    innerOsMode: $('#inner-os-mode'),
    innerMorningMode: $('#inner-morning-mode'),
    intimacyVitals: $('#intimacy-vitals'),
    intimacyVitalsSource: $('#intimacy-vitals-source'),
    intimacyVitalsSize: $('#intimacy-vitals-size'),
    intimacyVitalsColor: $('#intimacy-vitals-color'),
    intimacyVitalsSwelling: $('#intimacy-vitals-swelling'),
    intimacyVitalsArousal: $('#intimacy-vitals-arousal'),
    intimacyVitalsHardness: $('#intimacy-vitals-hardness'),
    intimacyVitalsUrge: $('#intimacy-vitals-urge'),
    intimacyVitalsVolume: $('#intimacy-vitals-volume'),
    intimacyVitalsPhase: $('#intimacy-vitals-phase'),
    intimacyVitalsCount: $('#intimacy-vitals-count'),
    intimacyVitalsRefractory: $('#intimacy-vitals-refractory'),
    intimacyVitalsCycle: $('#intimacy-vitals-cycle'),
    intimacyVitalsEvent: $('#intimacy-vitals-event'),
    homeInnerHighlights: $('#home-inner-highlights'),
    homeGreeting: $('#home-greeting'),
    homeHeroNote: $('#home-hero-note'),
    homePresenceLabel: $('#home-presence-label'),
    homeChapter: $('#home-chapter'),
    homeChapterNote: $('#home-chapter-note'),
    homeInteractions: $('#home-interactions'),
    homeAxisStrip: $('#home-axis-strip'),
    homeLifePhase: $('#home-life-phase'),
    homeLifeActivity: $('#home-life-activity'),
    homeLifeEvent: $('#home-life-event'),
    homeThreadFocus: $('#home-thread-focus'),
    homeRecentSessions: $('#home-recent-sessions'),
    homeModel: $('#home-model'),
    homeHealthDot: $('#home-health-dot'),
    mobileLifePhase: $('#mobile-life-phase'),
    mobileInteractions: $('#mobile-interactions'),
    mobileSessionCount: $('#mobile-session-count'),
    mobileBrainState: $('#mobile-brain-state'),
    relationChapter: $('#relation-chapter'),
    relationNote: $('#relation-note'),
    relationInteractions: $('#relation-interactions'),
    relationLastSettlement: $('#relation-last-settlement'),
    relationAxisList: $('#relation-axis-list'),
    relationThreadList: $('#relation-thread-list'),
    relationThreadHistory: $('#relation-thread-history'),
    relationThreadHistoryCount: $('#relation-thread-history-count'),
    sharedWorldList: $('#shared-world-list'),
    relationMomentList: $('#relation-moment-list'),
    relationshipFoundation: $('#relationship-foundation'),
    foundationEnabled: $('#foundation-enabled'),
    foundationCount: $('#foundation-count'),
    foundationStrategy: $('#foundation-strategy'),
    foundationStatus: $('#foundation-status'),
    btnFoundationSave: $('#btn-foundation-save'),
    btnFoundationImport: $('#btn-foundation-import'),
    foundationFileInput: $('#foundation-file-input'),
    continuityEnabled: $('#continuity-enabled'),
    continuityAutoThreads: $('#continuity-auto-threads'),
    continuitySharedContext: $('#continuity-shared-context'),
    continuityMoments: $('#continuity-moments'),
    btnContinuitySave: $('#btn-continuity-save'),
    btnAddThread: $('#btn-add-thread'),
    btnAddShared: $('#btn-add-shared'),
    continuityDialog: $('#continuity-dialog'),
    continuityForm: $('#continuity-form'),
    continuityDialogTitle: $('#continuity-dialog-title'),
    continuityKind: $('#continuity-kind'),
    continuityTitle: $('#continuity-title'),
    continuityDetail: $('#continuity-detail'),
    continuityDialogClose: $('#continuity-dialog-close'),
    continuityCancel: $('#continuity-cancel'),
    continuitySubmit: $('#continuity-submit'),
    memoryQuery: $('#memory-query'),
    btnMemorySearch: $('#btn-memory-search'),
    memoryResults: $('#memory-results'),
    memoryCount: $('#memory-count'),
    btnConversationImport: $('#btn-conversation-import'),
    conversationImportInput: $('#conversation-import-input'),
    conversationImportPreview: $('#conversation-import-preview'),
    btnExportCurrent: $('#btn-export-current'),
    btnExportAll: $('#btn-export-all'),
    chatSearchQuery: $('#chat-search-query'),
    btnChatSearch: $('#btn-chat-search'),
    chatSearchResults: $('#chat-search-results'),
    btnFavoritesRefresh: $('#btn-favorites-refresh'),
    favoriteResults: $('#favorite-results'),
    btnStyleFromSession: $('#btn-style-from-session'),
    styleProfileEmpty: $('#style-profile-empty'),
    styleProfileEditor: $('#style-profile-editor'),
    styleProfileName: $('#style-profile-name'),
    styleProfileEnabled: $('#style-profile-enabled'),
    styleProfileInstructions: $('#style-profile-instructions'),
    styleProfileExamples: $('#style-profile-examples'),
    btnStyleSave: $('#btn-style-save'),
    btnStyleUndo: $('#btn-style-undo'),
    btnStyleDelete: $('#btn-style-delete'),
    btnLocalBackup: $('#btn-local-backup'),
    btnLocalRestore: $('#btn-local-restore'),
    localRestoreInput: $('#local-restore-input'),
    localRestoreStatus: $('#local-restore-status'),
    btnLocalServiceInstall: $('#btn-local-service-install'),
    btnLocalServiceRemove: $('#btn-local-service-remove'),
    localServiceStatus: $('#local-service-status'),
    chatHistoryNav: $('#chat-history-nav'),
    chatJumpStart: $('#chat-jump-start'),
    chatJumpEnd: $('#chat-jump-end'),
    chatHistoryTrack: $('#chat-history-track'),
    chatHistoryFill: $('#chat-history-fill'),
    chatHistoryThumb: $('#chat-history-thumb'),
    chatHistoryPercent: $('#chat-history-percent'),
  };

  const overlay = document.createElement('div');
  overlay.className = 'sidebar-overlay';
  // The sidebar lives inside #app, which is its own stacking context.  Keeping
  // the overlay on <body> put it above the whole app, so Safari blurred the
  // sidebar together with the page even though the sidebar had a higher local
  // z-index.  Make them siblings so 40 (overlay) < 50 (sidebar) works as
  // intended: only the page behind the drawer is softened.
  (document.getElementById('app') || document.body).appendChild(overlay);

  // Markdown 只负责呈现模型已经输出的可见文字。所有 HTML 都先经过
  // DOMPurify；远程图片与可执行/交互标签默认禁止，避免一条回复触发外链
  // 跟踪或脚本执行。
  const markdownEngine = window.marked?.parse ? window.marked : window.marked?.marked;
  if (markdownEngine?.setOptions) {
    markdownEngine.setOptions({ gfm: true, breaks: true, async: false });
  }

  function renderMarkdown(text) {
    const source = String(text || '');
    if (!source) return '';
    if (!markdownEngine?.parse || !window.DOMPurify?.sanitize) {
      return esc(source).replace(/\n/g, '<br>');
    }
    try {
      const rendered = markdownEngine.parse(source);
      return window.DOMPurify.sanitize(rendered, {
        USE_PROFILES: { html: true },
        FORBID_TAGS: ['script', 'style', 'iframe', 'object', 'embed', 'form', 'input', 'button', 'img', 'video', 'audio'],
        FORBID_ATTR: ['style', 'srcset', 'onerror', 'onload'],
      });
    } catch (error) {
      console.warn('Markdown 渲染失败，已回退为纯文字:', error);
      return esc(source).replace(/\n/g, '<br>');
    }
  }

  function decorateRenderedMarkdown(bubble) {
    bubble?.querySelectorAll('a').forEach((link) => {
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
    });
  }

  // ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  //  初始化
  // ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  async function init() {
    const accessReady = await (window.__daxiguaAccessReady || Promise.resolve(true));
    if (!accessReady) return;
    organizeSystemSections();
    LEGACY_THEME_KEYS.forEach((key) => localStorage.removeItem(key));
    syncFlowerSeaControls();
    syncFontControls();
    syncTextScale();
    bindEvents();
    await loadSessions();
    await loadProviders();
    await loadModelsForProvider(state.activeProvider);
    await refreshHealth();
    await loadStickerLibrary();
    await loadVoiceSettings();
    await loadOceanListenStatus();
    setInterval(refreshHealth, 30000);
    registerSW();

    // 没有session就创建一个
    const requestedSession = new URLSearchParams(window.location.search).get('session');
    const requestedExists = requestedSession && state.sessions.some((item) => item.id === requestedSession);
    if (state.sessions.length === 0) {
      newSession(false);
    } else if (requestedExists) {
      await switchSession(requestedSession, false);
    } else {
      await switchSession(state.sessions[0].id, false);
    }
    await reconcilePendingChatRequest({ startup: true });
    startCoPresenceRuntime();
    switchView(requestedExists ? 'chat' : 'home');
    requestAnimationFrame(updateChatHistoryProgress);
    await loadHomeData();
    await resumeConversationImport();
  }

  function organizeSystemSections() {
    const target = $('#inner-content');
    if (!target) return;
    ['#inner-state-section', '#inner-advanced-source'].forEach((selector) => {
      const section = $(selector);
      if (section) target.appendChild(section);
    });
    // v8.3: expose already-built features as direct, balanced pages. This is
    // DOM-only and never enters the model request/cache path.
    window.JTYUI83?.organize?.();
    window.JTYSFX?.bind?.();
  }

  // ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  //  事件绑定
  // ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  function bindEvents() {
    syncMobileVisualViewport();
    window.visualViewport?.addEventListener('resize', syncMobileVisualViewport);
    window.visualViewport?.addEventListener('scroll', syncMobileVisualViewport);
    window.addEventListener('resize', syncMobileVisualViewport);
    window.addEventListener('orientationchange', syncMobileVisualViewport);
    window.addEventListener('pageshow', handlePageShow);
    window.addEventListener('online', handleOnlineResume);
    document.addEventListener('focusin', syncMobileVisualViewport);
    document.addEventListener('focusout', syncMobileVisualViewport);

    // 发送
    dom.btnSend.addEventListener('click', () => {
      if (state.isStreaming || state.recoveringPendingRequest) stopGeneration();
      else sendMessage();
    });
    dom.messages.addEventListener('click', handleMessageAction);
    dom.messages.addEventListener('scroll', updateChatHistoryProgress, { passive: true });
    dom.chatJumpStart?.addEventListener('click', jumpToConversationStart);
    dom.chatJumpEnd?.addEventListener('click', jumpToConversationEnd);
    dom.chatHistoryTrack?.addEventListener('click', jumpWithinConversationHistory);
    dom.input.addEventListener('keydown', (e) => {
      presence.inputMethod = 'keyboard';
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && !presence.composing) {
        e.preventDefault();
        sendMessage();
      }
    });
    dom.input.addEventListener('compositionstart', () => {
      presence.composing = true;
    });
    dom.input.addEventListener('compositionend', () => {
      presence.composing = false;
      handlePresenceInput();
    });
    dom.input.addEventListener('paste', () => {
      presence.pastes += 1;
      presence.inputMethod = 'paste';
    });
    dom.input.addEventListener('input', (event) => {
      updateSendState();
      autoResize(dom.input);
      if (!event.isComposing && !presence.composing) handlePresenceInput();
    });
    document.addEventListener('visibilitychange', handlePresenceVisibility);
    window.addEventListener('pagehide', () => sendPresenceVisibility('hidden', true));
    dom.btnAttach?.addEventListener('click', () => dom.attachmentInput?.click());
    dom.attachmentInput?.addEventListener('change', handleAttachmentSelection);
    dom.btnFileWorkspace?.addEventListener('click', openFileWorkspaceDialog);
    dom.btnVoiceMessage?.addEventListener('click', toggleVoiceMessageCapture);
    dom.btnVoiceCall?.addEventListener('click', () => {
      if (window.DaxiguaVoiceHost?.open) window.DaxiguaVoiceHost.open();
      else openVoiceCall();
    });
    dom.voiceCallClose?.addEventListener('click', endVoiceCall);
    dom.voiceCallEnd?.addEventListener('click', endVoiceCall);
    dom.voiceCallRoute?.addEventListener('click', toggleVoiceRoute);
    dom.voiceCallDialog?.addEventListener('cancel', (event) => {
      event.preventDefault();
      endVoiceCall();
    });
    window.addEventListener('daxigua-native-back', () => {
      if (window.DaxiguaVoiceHost?.handleBack) window.DaxiguaVoiceHost.handleBack();
      else endVoiceCall();
    });
    window.addEventListener('daxigua-native-hangup', () => {
      if (window.DaxiguaVoiceHost?.end) window.DaxiguaVoiceHost.end('native-notification');
      else endVoiceCall();
    });
    window.addEventListener('pagehide', finishVoiceCallOnPageExit);
    dom.voiceCallTalk?.addEventListener('contextmenu', (event) => event.preventDefault());
    dom.voiceCallTalk?.addEventListener('pointerdown', (event) => {
      event.preventDefault();
      dom.voiceCallTalk.setPointerCapture?.(event.pointerId);
      startVoiceCapture('call');
    });
    ['pointerup', 'pointercancel', 'lostpointercapture'].forEach((eventName) => {
      dom.voiceCallTalk?.addEventListener(eventName, (event) => {
        event.preventDefault();
        stopVoiceCapture();
      });
    });
    dom.btnWorkspaceManage?.addEventListener('click', openFileWorkspaceDialog);
    dom.btnWorkspaceUpload?.addEventListener('click', () => dom.attachmentInput?.click());
    dom.btnWorkspaceDialogUpload?.addEventListener('click', () => dom.attachmentInput?.click());
    dom.btnWorkspaceRefresh?.addEventListener('click', () => loadFileWorkspace(dom.workspaceSearch?.value || ''));
    dom.btnOceanInstall?.addEventListener('click', installOceanListen);
    dom.btnOceanRefresh?.addEventListener('click', async () => {
      await loadOceanListenStatus();
      await refreshPendingAudioAnalyses();
    });
    dom.workspaceSearch?.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') { event.preventDefault(); loadFileWorkspace(dom.workspaceSearch.value); }
    });
    dom.workspaceDialogSearch?.addEventListener('input', () => loadFileWorkspace(dom.workspaceDialogSearch.value));
    dom.workspaceDialogClose?.addEventListener('click', closeFileWorkspaceDialog);
    dom.workspaceDialog?.addEventListener('click', (event) => {
      if (event.target === dom.workspaceDialog) closeFileWorkspaceDialog();
    });
    dom.contextBudgetPill?.addEventListener('click', openContextInspector);
    dom.contextInspectorClose?.addEventListener('click', closeContextInspector);
    dom.contextInspectorDialog?.addEventListener('click', (event) => {
      if (event.target === dom.contextInspectorDialog) closeContextInspector();
    });
    dom.contextCompressionEnabled?.addEventListener('change', updateContextCompressionEnabled);
    dom.btnContextCompact?.addEventListener('click', runContextCompaction);
    dom.btnContextRebuild?.addEventListener('click', rebuildContextCompaction);
    dom.fontMode?.addEventListener('change', updateFontMode);
    dom.textScale?.addEventListener('change', updateTextScale);
    dom.consoleNav?.addEventListener('click', onConsoleNavClick);

    // 侧边栏
    dom.btnMenu.addEventListener('click', toggleSidebar);
    overlay.addEventListener('click', closeSidebar);
    let sessionSearchTimer = null;
    dom.sessionSearch?.addEventListener('input', () => {
      clearTimeout(sessionSearchTimer);
      sessionSearchTimer = setTimeout(() => loadSessions(), 220);
    });
    dom.btnSessionMore?.addEventListener('click', () => loadSessions({ append: true }));

    // 新对话
    dom.btnNewSession.addEventListener('click', () => {
      newSession();
      closeSidebar();
    });

    $$('.btn-nav[data-view]').forEach((button) => {
      button.addEventListener('click', () => {
        switchView(button.dataset.view || 'home');
        closeSidebar();
      });
    });
    $$('[data-go-view]').forEach((button) => {
      button.addEventListener('click', () => switchView(button.dataset.goView || 'home'));
    });
    $('#btn-home-chat')?.addEventListener('click', () => switchView('chat'));
    $('#btn-home-us')?.addEventListener('click', () => switchView('us'));
    $('#btn-home-new-session')?.addEventListener('click', () => newSession(true));
    $('#btn-mobile-new-session')?.addEventListener('click', () => newSession(true));

    dom.btnFlowerSea?.addEventListener('click', activateFlowerSeaMode);
    // 晴天、雨天、水波是同一个互斥环境选择；水波内部自动启动折射。
    window.addEventListener('daxigua:flower-sea-exit', () => setFlowerSea(false));

    dom.btnModelHub?.addEventListener('click', async () => {
      switchView('system');
      requestAnimationFrame(() => {
        window.JTYUI83?.system?.activate?.('settings');
        requestAnimationFrame(() => $('#model-hub-section')?.scrollIntoView({behavior:'auto',block:'start'}));
      });
      await loadConsoleData();
    });
    dom.btnRefreshModels?.addEventListener('click', refreshProviderConfiguration);
    dom.keyCredential?.addEventListener('change', renderKeyManagerStatus);
    dom.keyValue?.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        saveKeyFromUI();
      }
    });
    dom.btnKeyToggle?.addEventListener('click', toggleKeyVisibility);
    dom.btnKeyClear?.addEventListener('click', clearKeyFromUI);
    dom.btnKeySave?.addEventListener('click', saveKeyFromUI);
    dom.btnBrainPreview?.addEventListener('click', runBrainIntegrityPreview);
    dom.modelSearch?.addEventListener('input', renderModelList);
    dom.btnApplyCustomModel?.addEventListener('click', () => {
      const model = dom.customModelId.value.trim();
      if (model) selectModel(model);
    });
    dom.customModelId?.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        const model = dom.customModelId.value.trim();
        if (model) selectModel(model);
      }
    });

    dom.btnDiagnosticRefresh?.addEventListener('click', loadDiagnosticsData);
    $('#btn-api-usage-refresh')?.addEventListener('click', () => loadApiUsageStats());
    $('#api-usage-range')?.addEventListener('click', (event) => {
      const button = event.target.closest('[data-usage-period]');
      if (!button) return;
      state.apiUsagePeriod = button.dataset.usagePeriod || '7d';
      $('#api-usage-range')?.querySelectorAll('[data-usage-period]').forEach((item) => {
        item.classList.toggle('active', item === button);
      });
      loadApiUsageStats();
    });
    $('#btn-honesty-refresh')?.addEventListener('click', loadRelationalHonestyData);
    dom.honestyEnabled?.addEventListener('change', saveRelationalHonestySettings);
    dom.btnSelfTest?.addEventListener('click', runDiagnosticSelfTest);
    dom.btnClearErrors?.addEventListener('click', clearDiagnosticErrors);
    dom.btnCoreRefresh?.addEventListener('click', loadCoreData);
    dom.btnCoreReset?.addEventListener('click', resetCoreState);
    dom.coreIntimacyMode?.addEventListener('change', updateCoreSettings);
    dom.btnArchiveSearch?.addEventListener('click', searchRawArchive);
    dom.archiveQuery?.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); searchRawArchive(); }
    });
    dom.btnImportSticker?.addEventListener('click', () => dom.stickerInput?.click());
    dom.stickerInput?.addEventListener('change', handleStickerImport);
    dom.btnRefreshVoices?.addEventListener('click', refreshVoiceOptions);
    dom.btnSaveVoice?.addEventListener('click', () => saveVoiceSettings());
    dom.btnTestVoice?.addEventListener('click', testVoice);
    dom.btnGenerateGreeting?.addEventListener('click', generateVoiceGreeting);
    dom.voiceModel?.addEventListener('change', updateVoiceSpeedSupport);
    [
      [dom.voiceStability, dom.voiceStabilityValue],
      [dom.voiceSimilarity, dom.voiceSimilarityValue],
      [dom.voiceStyle, dom.voiceStyleValue],
      [dom.voiceSpeed, dom.voiceSpeedValue],
    ].forEach(([input, output]) => input?.addEventListener('input', () => {
      if (output) output.textContent = Number(input.value || 0).toFixed(2);
    }));
    dom.btnCharacterSave?.addEventListener('click', saveCharacterSettings);
    dom.btnCharacterLab?.addEventListener('click', loadCharacterData);
    dom.btnLivingTick?.addEventListener('click', tickLivingState);
    dom.btnLivingReset?.addEventListener('click', resetLivingState);
    dom.btnEnablePush?.addEventListener('click', enablePushNotifications);
    dom.btnTestInitiative?.addEventListener('click', testIndependentInitiative);
    dom.btnTestMorningTrigger?.addEventListener('click', testMorningTrigger);
    dom.btnAddThread?.addEventListener('click', () => openContinuityDialog('thread'));
    dom.btnAddShared?.addEventListener('click', () => openContinuityDialog('shared'));
    dom.btnContinuitySave?.addEventListener('click', saveRelationshipSettings);
    dom.btnFoundationSave?.addEventListener('click', saveRelationshipFoundation);
    dom.btnFoundationImport?.addEventListener('click', () => dom.foundationFileInput?.click());
    dom.foundationFileInput?.addEventListener('change', importRelationshipFoundation);
    dom.relationshipFoundation?.addEventListener('input', updateFoundationCount);
    dom.continuityDialogClose?.addEventListener('click', closeContinuityDialog);
    dom.continuityCancel?.addEventListener('click', closeContinuityDialog);
    dom.continuityForm?.addEventListener('submit', submitContinuityDialog);
    dom.continuityDialog?.addEventListener('click', (event) => {
      if (event.target === dom.continuityDialog) closeContinuityDialog();
    });
    dom.btnMemorySearch?.addEventListener('click', searchMemoryArchive);
    dom.btnNaturalMemoryRefresh?.addEventListener('click', loadNaturalFacts);
    dom.btnConversationImport?.addEventListener('click', () => dom.conversationImportInput?.click());
    dom.conversationImportInput?.addEventListener('change', previewConversationImport);
    dom.btnExportCurrent?.addEventListener('click', () => downloadConversationExport(false));
    dom.btnExportAll?.addEventListener('click', () => downloadConversationExport(true));
    dom.btnChatSearch?.addEventListener('click', searchAllMessages);
    dom.chatSearchQuery?.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') { event.preventDefault(); searchAllMessages(); }
    });
    dom.btnFavoritesRefresh?.addEventListener('click', loadFavoriteMessages);
    dom.btnStyleFromSession?.addEventListener('click', createStyleProfileFromCurrent);
    dom.btnStyleSave?.addEventListener('click', saveStyleProfile);
    dom.btnStyleUndo?.addEventListener('click', undoStyleProfile);
    dom.btnStyleDelete?.addEventListener('click', deleteStyleProfile);
    dom.btnLocalBackup?.addEventListener('click', downloadLocalBackup);
    dom.btnLocalRestore?.addEventListener('click', () => dom.localRestoreInput?.click());
    dom.localRestoreInput?.addEventListener('change', stageLocalRestore);
    dom.btnLocalServiceInstall?.addEventListener('click', installLocalService);
    dom.btnLocalServiceRemove?.addEventListener('click', removeLocalService);
    dom.memoryQuery?.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') { event.preventDefault(); searchMemoryArchive(); }
    });
    [dom.livingEnabled, dom.livingDreams, dom.livingMorning].forEach((el) => {
      el?.addEventListener('change', saveLivingSettings);
    });
    [dom.coPresenceNatural, dom.coPresenceIndependent, dom.coPresenceRhythm].forEach((el) => {
      el?.addEventListener('change', saveCoPresenceSettings);
    });
    dom.btnInnerRefresh?.addEventListener('click', loadInnerStateData);
    document.querySelectorAll('[data-inner-domain]').forEach((button) => {
      button.addEventListener('click', () => {
        state.activeInnerDomain = button.dataset.innerDomain || 'emotion';
        renderInnerState(state.innerState || {});
      });
    });
    [
      dom.innerVisibleChanges,
      dom.innerDetailMode,
      dom.innerEmotionTakeover,
      dom.innerIntimacyEnabled,
      dom.innerIntimacyVitalsVisible,
      dom.innerMorningTakeover,
      dom.innerMorningProactive,
      dom.innerReflectionMode,
      dom.innerIntimacyMode,
      dom.innerOsMode,
      dom.innerMorningMode,
    ].forEach((el) => {
      el?.addEventListener('change', saveInnerStateSettings);
    });
    dom.stickerMode?.addEventListener('change', () => {
      state.stickerMode = dom.stickerMode.value || 'off';
      localStorage.setItem('companion:sticker-mode', state.stickerMode);
      saveBrainOptionsFromUI();
    });

    [dom.reasoningEffort, dom.thinkingMode, dom.reasoningContext, dom.thinkingBudget, dom.thinkingVisibility, dom.verbosity, dom.maxOutputTokens].forEach((el) => {
      el?.addEventListener('change', saveBrainOptionsFromUI);
    });

    // 长按菜单
    document.addEventListener('click', (e) => {
      if (!dom.contextMenu.contains(e.target)) {
        dom.contextMenu.classList.add('hidden');
      }
    });
    dom.contextMenu.addEventListener('click', handleContextMenu);
  }

  // ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  //  v7.4 共处感知（只发节奏数字，永不发送草稿文字或长度）
  // ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  function resetPresenceTracker() {
    clearTimeout(presence.pauseTimer);
    presence.pauseTimer = null;
    presence.active = false;
    presence.startedAt = 0;
    presence.lastInputAt = 0;
    presence.lastLength = 0;
    presence.lastSignalAt = 0;
    presence.revisions = 0;
    presence.pauses = 0;
    presence.clears = 0;
    presence.pastes = 0;
    presence.inputEvents = 0;
    presence.deletions = 0;
    presence.bursts = 0;
    presence.longestPauseMs = 0;
    presence.inputMethod = 'keyboard';
    presence.sessionId = state.currentSession || '';
  }

  function presenceMetrics(draftActive = false) {
    const now = Date.now();
    return {
      active_ms: presence.startedAt ? Math.max(0, now - presence.startedAt) : 0,
      revision_count: presence.revisions,
      pause_count: presence.pauses,
      clear_count: presence.clears,
      paste_count: presence.pastes,
      input_event_count: presence.inputEvents,
      deletion_count: presence.deletions,
      burst_count: presence.bursts,
      longest_pause_ms: presence.longestPauseMs,
      input_method: presence.inputMethod || 'unknown',
      draft_active: Boolean(draftActive),
    };
  }

  function postPresenceEvent(event, metrics = {}, beacon = false, sessionId = '') {
    const targetSession = sessionId || state.currentSession;
    if (!targetSession) return;
    const payload = {
      session_id: targetSession,
      event,
      metrics: {
        active_ms: Number(metrics.active_ms || 0),
        revision_count: Number(metrics.revision_count || 0),
        pause_count: Number(metrics.pause_count || 0),
        clear_count: Number(metrics.clear_count || 0),
        paste_count: Number(metrics.paste_count || 0),
        input_event_count: Number(metrics.input_event_count || 0),
        deletion_count: Number(metrics.deletion_count || 0),
        burst_count: Number(metrics.burst_count || 0),
        longest_pause_ms: Number(metrics.longest_pause_ms || 0),
        input_method: String(metrics.input_method || 'unknown'),
        draft_active: Boolean(metrics.draft_active),
      },
    };
    try {
      if (beacon && navigator.sendBeacon) {
        navigator.sendBeacon(
          '/api/co-presence/event',
          new Blob([JSON.stringify(payload)], { type: 'application/json' }),
        );
        return;
      }
      fetch('/api/co-presence/event', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
        keepalive: true,
      }).then((response) => response.ok ? response.json() : null)
        .then((data) => {
          if (data?.state) {
            state.coPresence = data.state;
            renderCoPresenceState(data.state);
          }
        }).catch(() => {});
    } catch (_) { /* 共处增强失败不影响聊天 */ }
  }

  function schedulePresencePause() {
    clearTimeout(presence.pauseTimer);
    presence.pauseTimer = setTimeout(() => {
      if (!dom.input.value || document.hidden || presence.composing) return;
      presence.pauses += 1;
      postPresenceEvent('paused', presenceMetrics(true));
    }, 6500);
  }

  function handlePresenceInput() {
    if (!dom.input || presence.composing) return;
    const now = Date.now();
    const currentLength = dom.input.value.length;
    if (state.currentSession) sessionDrafts.set(state.currentSession, dom.input.value);
    presence.sessionId = state.currentSession || '';
    const previousLength = presence.lastLength;
    presence.inputEvents += 1;
    if (!presence.active && currentLength > 0) {
      presence.active = true;
      presence.startedAt = now;
      presence.lastInputAt = now;
      presence.lastLength = currentLength;
      presence.lastSignalAt = now;
      presence.bursts = 1;
      postPresenceEvent('speaking', presenceMetrics(true));
      schedulePresencePause();
      return;
    }
    if (presence.active && presence.lastInputAt) {
      const gap = Math.max(0, now - presence.lastInputAt);
      presence.longestPauseMs = Math.max(presence.longestPauseMs, gap);
      if (gap > 6000) presence.pauses += 1;
      if (gap > 1400) presence.bursts += 1;
    }
    if (currentLength < previousLength) {
      presence.revisions += 1;
      presence.deletions += Math.max(1, previousLength - currentLength);
    }
    presence.lastInputAt = now;
    presence.lastLength = currentLength;
    if (previousLength > 0 && currentLength === 0) {
      presence.clears += 1;
      const snapshot = presenceMetrics(false);
      postPresenceEvent('cleared', snapshot);
      resetPresenceTracker();
      return;
    }
    if (currentLength > 0) {
      if (now - presence.lastSignalAt >= 3500) {
        presence.lastSignalAt = now;
        postPresenceEvent('speaking', presenceMetrics(true));
      }
      schedulePresencePause();
    }
  }

  function finishPresenceForSubmit(inputMethod = '') {
    if (inputMethod) presence.inputMethod = inputMethod;
    const snapshot = presenceMetrics(false);
    resetPresenceTracker();
    return snapshot;
  }

  function sendPresenceVisibility(event, beacon = false) {
    const hasDraft = Boolean(dom.input?.value);
    postPresenceEvent(
      event,
      { ...presenceMetrics(hasDraft), draft_active: hasDraft },
      beacon,
    );
  }

  function handlePresenceVisibility() {
    sendPresenceVisibility(document.hidden ? 'hidden' : 'visible');
    if (!document.hidden) reconcilePendingChatRequest({ foreground: true });
  }

  let viewportFrame = 0;
  function syncMobileVisualViewport() {
    cancelAnimationFrame(viewportFrame);
    viewportFrame = requestAnimationFrame(() => {
      const root = document.documentElement;
      const viewport = window.visualViewport;
      if (!viewport || window.innerWidth > 768) {
        root.style.removeProperty('--daxigua-viewport-height');
        delete root.dataset.keyboardOpen;
        return;
      }
      root.style.setProperty(
        '--daxigua-viewport-height',
        `${Math.max(320, Math.round(viewport.height))}px`,
      );
      const active = document.activeElement;
      const editable = Boolean(active && /^(INPUT|TEXTAREA|SELECT)$/.test(active.tagName));
      root.dataset.keyboardOpen = editable && viewport.height < window.innerHeight * 0.82
        ? 'true'
        : 'false';
    });
  }

  async function refreshForegroundState(reason = {}) {
    const providerRefresh = loadProviders(false)
      .then(() => loadModelsForProvider(state.activeProvider));
    await Promise.allSettled([
      providerRefresh,
      refreshHealth(),
      reconcilePendingChatRequest(reason),
      pollNaturalMessages(),
    ]);
  }

  async function handlePageShow(event) {
    syncMobileVisualViewport();
    if (!event.persisted) return;
    sendPresenceVisibility('visible');
    await refreshForegroundState({ bfcache: true });
    navigator.serviceWorker?.getRegistration?.()
      .then((registration) => registration?.update())
      .catch(() => {});
  }

  function handleOnlineResume() {
    refreshForegroundState({ online: true }).catch(() => {});
  }

  async function loadCoPresenceData() {
    try {
      const query = state.currentSession
        ? `?session_id=${encodeURIComponent(state.currentSession)}`
        : '';
      const response = await fetch(`/api/co-presence/state${query}`, {
        cache: 'no-store',
      });
      if (!response.ok) return;
      state.coPresence = await response.json();
      renderCoPresenceState(state.coPresence);
    } catch (_) { /* 控制台增强不阻断主界面 */ }
  }

  function renderCoPresenceState(data = {}) {
    const settings = data.settings || {};
    if (dom.livingContacts) {
      dom.livingContacts.textContent = data.status || '安静共处';
    }
    if (dom.coPresenceNatural) {
      dom.coPresenceNatural.value = String(
        settings.natural_continuation_enabled !== false
      );
    }
    if (dom.coPresenceIndependent) {
      dom.coPresenceIndependent.value = String(
        settings.independent_initiative_enabled !== false
      );
    }
    if (dom.coPresenceRhythm) {
      dom.coPresenceRhythm.value = String(settings.rhythm_enabled !== false);
    }
    if (dom.coPresencePrivacy) {
      dom.coPresencePrivacy.textContent = data.privacy
        || '只感知停顿、删改与清空；从不读取或上传未发送文字。';
    }
    const rhythm = data.rhythm || {};
    if (dom.coPresenceRhythmSummary) {
      const seconds = Math.round(Number(rhythm.active_ms || 0) / 1000);
      dom.coPresenceRhythmSummary.textContent = rhythm.input_method
        ? `最近节奏：${seconds}s · ${Number(rhythm.input_event_count || 0)} 次输入 · ${Number(rhythm.revision_count || 0)} 次删改 · ${Number(rhythm.pause_count || 0)} 次停顿`
        : '还没有记录到这个窗口的表达节奏。';
    }
    if (dom.coPresenceRules) {
      const rules = Array.isArray(data.hard_rules) ? data.hard_rules : [];
      dom.coPresenceRules.innerHTML = rules.length
        ? rules.map((rule) => `<span class="co-presence-rule">${esc(rule)}</span>`).join('')
        : '';
    }
    if (dom.coPresenceTimeline) {
      const timeline = Array.isArray(data.timeline) ? data.timeline : [];
      dom.coPresenceTimeline.innerHTML = timeline.length ? timeline.map((item) => `
        <div class="diagnostic-row static co-presence-log is-${esc(item.status || 'info')}">
          <span>${esc(item.detail || item.kind || '')}</span>
          <small>${esc(item.kind || '')}${item.event_id ? ` · #${Number(item.event_id)}` : ''}</small>
          <em>${esc(item.created_at ? formatShortDate(item.created_at) : '')}</em>
        </div>`).join('') : '<div class="diagnostic-empty">这里会显示“感知到什么、模型如何决定、为什么没有发送”，不会显示草稿内容。</div>';
    }
  }

  async function saveCoPresenceSettings() {
    try {
      const response = await fetch('/api/co-presence/settings', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          enabled: true,
          natural_continuation_enabled: boolValue(dom.coPresenceNatural),
          independent_initiative_enabled: boolValue(dom.coPresenceIndependent),
          rhythm_enabled: boolValue(dom.coPresenceRhythm),
        }),
      });
      if (!response.ok) throw new Error('保存失败');
      state.coPresence = await response.json();
      renderCoPresenceState(state.coPresence);
    } catch (error) {
      alert(`共处感知设置保存失败：${error.message}`);
    }
  }

  async function testIndependentInitiative() {
    if (!state.currentSession) {
      alert('先打开一个已经保存的对话窗口。');
      return;
    }
    const button = dom.btnTestInitiative;
    if (button) { button.disabled = true; button.textContent = '正在建立测试…'; }
    try {
      const response = await fetch('/api/co-presence/test-initiative', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: state.currentSession }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || '测试事件建立失败');
      if (dom.livingContacts) dom.livingContacts.textContent = '主动消息测试已排队';
      await loadCoPresenceData();
      setTimeout(() => pollNaturalMessages().catch(() => {}), 3500);
    } catch (error) {
      alert(`主动消息测试失败：${error.message}`);
    } finally {
      if (button) { button.disabled = false; button.textContent = '测试主动开口'; }
    }
  }

  async function testMorningTrigger() {
    if (!state.currentSession) {
      alert('先打开一个已经保存的对话窗口。');
      return;
    }
    const button = dom.btnTestMorningTrigger;
    if (button) { button.disabled = true; button.textContent = '正在测试链路…'; }
    try {
      const response = await fetch('/api/morning/test-trigger', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: state.currentSession }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || '晨间触发测试建立失败');
      if (dom.innerMorningNote) dom.innerMorningNote.textContent = '晨间主动表达测试已进入原窗口链路';
      await loadCoPresenceData();
      setTimeout(() => {
        pollNaturalMessages().catch(() => {});
        loadInnerStateData().catch(() => {});
      }, 3500);
    } catch (error) {
      alert(`晨间触发测试失败：${error.message}`);
    } finally {
      if (button) { button.disabled = false; button.textContent = '测试晨间触发'; }
    }
  }

  async function refreshPushStatus() {
    if (!dom.pushStatus) return;
    try {
      const response = await fetch('/api/push/status', { cache: 'no-store' });
      const server = await response.json();
      const permission = ('Notification' in window) ? Notification.permission : 'unsupported';
      if (!server.configured) {
        dom.pushStatus.textContent = '系统通知：后端尚未配置 VAPID 密钥；网页打开时仍会收到主动消息。';
      } else if (server.ready && permission === 'granted') {
        dom.pushStatus.textContent = `系统通知：已启用（${Number(server.active_subscriptions || 0)} 个有效订阅）。`;
      } else if (permission === 'denied') {
        dom.pushStatus.textContent = '系统通知：已被浏览器拒绝，需要在系统设置中重新允许。';
      } else {
        dom.pushStatus.textContent = '系统通知：尚未启用；网页关闭后不会弹出主动消息提醒。';
      }
    } catch (_) {
      dom.pushStatus.textContent = '系统通知：状态读取失败；网页内主动消息不受影响。';
    }
  }

  async function enablePushNotifications() {
    const button = dom.btnEnablePush;
    if (button) { button.disabled = true; button.textContent = '正在开启…'; }
    try {
      if (!window.isSecureContext) throw new Error('系统通知需要 HTTPS 或本机 localhost');
      if (!('Notification' in window) || !('serviceWorker' in navigator) || !('PushManager' in window)) {
        throw new Error('当前浏览器不支持网页推送');
      }
      const permission = await Notification.requestPermission();
      if (permission !== 'granted') throw new Error('没有获得通知权限');
      const reg = await navigator.serviceWorker.ready;
      await subscribePush(reg);
      await refreshPushStatus();
    } catch (error) {
      alert(`系统通知没有开启：${error.message}`);
      await refreshPushStatus();
    } finally {
      if (button) { button.disabled = false; button.textContent = '开启系统通知'; }
    }
  }

  async function pollNaturalMessages() {
    const activeSession = state.sessions.find((item) => item.id === state.currentSession);
    if (
      !state.currentSession
      || activeSession?.persisted === false
      || state.loadedSessionId !== state.currentSession
      || state.isStreaming
      || !state.historyWindowAtLatest
      || document.hidden
    ) return;
    const sessionId = state.currentSession;
    const afterId = Math.max(0, Number(state.latestMessageId || 0));
    try {
      const response = await fetch(
        `/api/sessions/${encodeURIComponent(sessionId)}/new-messages?after_id=${afterId}&limit=50`,
        { cache: 'no-store' },
      );
      if (!response.ok) return;
      const data = await response.json();
      if (sessionId !== state.currentSession) return;
      const nearBottom = (
        dom.messages.scrollHeight - dom.messages.scrollTop
        - dom.messages.clientHeight < 180
      );
      (data.items || []).forEach((message) => {
        const messageId = Number(message.id || 0);
        state.latestMessageId = Math.max(state.latestMessageId, messageId);
        if (!['user', 'assistant'].includes(message.role)) return;
        if (dom.messages.querySelector(`[data-message-id="${messageId}"]`)) return;
        appendMessage(
          message.role,
          message.content,
          message.cost,
          {
            ...(message.metadata || {}),
            message_id: messageId,
            history_position: message.history_position,
            is_favorite: Boolean(message.is_favorite),
          },
        );
      });
      state.latestMessageId = Math.max(
        state.latestMessageId,
        Number(data.latest_id || 0),
      );
      syncLatestHistoryPositions(Number(data.total_count || state.totalMessageCount));
      trimLatestHistoryDomFromTop(sessionId);
      if (nearBottom && (data.items || []).length) scrollToBottom();
      if ((data.items || []).length) {
        loadSessions().catch(() => {});
        loadCoPresenceData().catch(() => {});
      }
    } catch (_) { /* 下一轮轮询自然恢复 */ }
  }

  function startCoPresenceRuntime() {
    clearInterval(presence.heartbeatTimer);
    presence.heartbeatTimer = setInterval(() => {
      if (!document.hidden && dom.input?.value && !presence.composing
          && presence.sessionId === state.currentSession) {
        presence.lastSignalAt = Date.now();
        postPresenceEvent('speaking', presenceMetrics(true));
      }
    }, 4000);
    clearInterval(state.naturalMessagePollTimer);
    state.naturalMessagePollTimer = setInterval(pollNaturalMessages, 6000);
    clearInterval(state.intimacyVitalsPollTimer);
    state.intimacyVitalsPollTimer = setInterval(() => {
      if (!document.hidden) loadIntimacyVitals().catch(() => {});
    }, 10000);
    sendPresenceVisibility('visible');
    loadCoPresenceData().catch(() => {});
    pollNaturalMessages().catch(() => {});
    loadIntimacyVitals().catch(() => {});
  }

  // ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  //  Session 管理
  // ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  async function loadSessions({ append = false } = {}) {
    try {
      const query = dom.sessionSearch?.value.trim() || '';
      const offset = append ? state.sessions.length : 0;
      const limit = state.sessionPageSize;
      const resp = await fetch(`/api/sessions?limit=${limit}&offset=${offset}&query=${encodeURIComponent(query)}`);
      const rawPage = await resp.json();
      if (!resp.ok || !Array.isArray(rawPage)) throw new Error('会话列表格式异常');
      const page = rawPage.map((item) => ({ ...item, persisted: true }));
      if (append) {
        const known = new Set(state.sessions.map((item) => item.id));
        state.sessions.push(...page.filter((item) => !known.has(item.id)));
      } else {
        state.sessions = page;
      }
      state.sessionHasMore = page.length === limit;
      const current = state.sessions.find((item) => item.id === state.currentSession);
      if (
        current
        && state.loadedSessionId === state.currentSession
        && state.historyWindowAtLatest
      ) {
        syncLatestHistoryPositions(Number(current.message_count || 0));
      }
      renderSessionList();
    } catch (e) {
      console.error('加载session失败:', e);
    }
  }

  function renderSessionList() {
    dom.sessionList.innerHTML = state.sessions.map((s) => `
      <div class="session-item ${s.id === state.currentSession ? 'active' : ''}" 
           data-id="${esc(s.id)}">
        <span class="session-title">${esc(s.title || '新对话')}</span>
        <span class="session-meta"><span>${s.message_count || 0} 条消息</span><span>$${(s.total_cost || 0).toFixed(4)}</span></span>
      </div>
    `).join('');
    dom.btnSessionMore?.classList.toggle('hidden', !state.sessionHasMore);

    // 点击切换
    dom.sessionList.querySelectorAll('.session-item').forEach((el) => {
      el.addEventListener('click', () => {
        switchSession(el.dataset.id);
        closeSidebar();
      });

      // 长按重命名
      let timer;
      el.addEventListener('touchstart', (e) => {
        timer = setTimeout(() => showContextMenu(e, el.dataset.id), 500);
      }, { passive: true });
      el.addEventListener('touchend', () => clearTimeout(timer));
      el.addEventListener('touchmove', () => clearTimeout(timer));

      // 桌面端右键
      el.addEventListener('contextmenu', (e) => {
        e.preventDefault();
        showContextMenu(e, el.dataset.id);
      });
    });
  }

  function leaveCurrentSession(nextSessionId = '') {
    const oldSession = state.currentSession || '';
    if (!oldSession || oldSession === nextSessionId) return;
    const draft = dom.input?.value || '';
    sessionDrafts.set(oldSession, draft);
    // Always close the old session's typing state before moving away.
    // Even an empty local input may correspond to a stale server-side draft_open
    // after a dropped/late heartbeat, so the end signal must not depend on `draft`.
    postPresenceEvent(
      'session_switched',
      { ...presenceMetrics(false), draft_active: false },
      true,
      oldSession,
    );
    clearTimeout(presence.pauseTimer);
    if (dom.input) dom.input.value = '';
  }

  function restoreSessionDraft(sessionId) {
    if (!dom.input) return;
    const draft = sessionDrafts.get(sessionId) || '';
    dom.input.value = draft;
    resetPresenceTracker();
    if (draft) {
      const now = Date.now();
      presence.active = true;
      presence.startedAt = now;
      presence.lastInputAt = now;
      presence.lastLength = draft.length;
      presence.lastSignalAt = now;
      presence.bursts = 1;
      presence.sessionId = sessionId;
      postPresenceEvent('speaking', presenceMetrics(true), false, sessionId);
      schedulePresencePause();
    }
  }

  function cancelHistoryNavigation() {
    state.historyNavigationEpoch += 1;
    state.historyRenderToken += 1;
    state.historyPageController?.abort();
    state.historyPageController = null;
    state.loadingOlderMessages = false;
    if (dom.messages) {
      dom.messages.style.scrollBehavior = '';
      dom.messages.style.overflowAnchor = '';
    }
    if (dom.chatJumpStart) {
      dom.chatJumpStart.disabled = false;
      dom.chatJumpStart.textContent = '↑';
    }
    if (dom.chatJumpEnd) {
      dom.chatJumpEnd.disabled = false;
      dom.chatJumpEnd.textContent = '↓';
    }
    if (dom.chatHistoryTrack) dom.chatHistoryTrack.disabled = false;
  }

  function resetHistoryMetrics() {
    state.totalMessageCount = 0;
    state.historyStartPosition = 0;
    state.historyEndPosition = 0;
    state.hasOlderMessages = false;
    state.historyWindowAtLatest = true;
    updateChatHistoryProgress();
  }

  function setMessageHistoryPosition(messageEl, position) {
    const normalized = Number(position || 0);
    if (!messageEl || !Number.isFinite(normalized) || normalized <= 0) return;
    messageEl.dataset.historyPosition = String(Math.trunc(normalized));
  }

  function refreshHistoryWindowPositions() {
    const positions = [...dom.messages.querySelectorAll('.message[data-history-position]')]
      .map((element) => Number(element.dataset.historyPosition || 0))
      .filter((value) => Number.isFinite(value) && value > 0);
    state.historyStartPosition = positions.length ? Math.min(...positions) : 0;
    state.historyEndPosition = positions.length ? Math.max(...positions) : 0;
  }

  function syncLatestHistoryPositions(totalCount) {
    const total = Math.max(0, Number(totalCount || 0));
    state.totalMessageCount = total;
    const messages = [...dom.messages.querySelectorAll('.message[data-message-id]')];
    const start = Math.max(1, total - messages.length + 1);
    messages.forEach((message, index) => {
      setMessageHistoryPosition(message, start + index);
    });
    refreshHistoryWindowPositions();
    if (messages.length) {
      const firstMessageId = Number(messages[0]?.dataset.messageId || 0);
      if (firstMessageId > 0) state.oldestMessageId = firstMessageId;
      const previouslyHadOlder = state.hasOlderMessages;
      state.hasOlderMessages = state.historyStartPosition > 1;
      const hasOlderControl = Boolean(
        dom.messages.querySelector('.older-messages-control:not(.latest-messages-control)'),
      );
      if (previouslyHadOlder !== state.hasOlderMessages
          || (state.hasOlderMessages && !hasOlderControl)) {
        renderOlderMessagesControl();
      }
    }
    updateChatHistoryProgress();
  }

  function applyHistoryPageMetrics(page) {
    state.totalMessageCount = Math.max(0, Number(page?.total_count || 0));
    state.oldestMessageId = Number(page?.oldest_id || 0);
    state.hasOlderMessages = Boolean(page?.has_more);
    state.historyStartPosition = Math.max(0, Number(page?.start_position || 0));
    state.historyEndPosition = Math.max(0, Number(page?.end_position || 0));
  }

  function renderHistoryMessagePage(page) {
    const messages = Array.isArray(page?.items) ? page.items : [];
    dom.messages.innerHTML = '';
    applyHistoryPageMetrics(page);
    state.latestMessageId = messages.reduce(
      (highest, item) => Math.max(highest, Number(item.id || 0)),
      0,
    );
    state.historyWindowAtLatest = !Boolean(page?.has_newer);
    state.loadedSessionId = state.currentSession;
    let visibleCount = 0;
    messages.forEach((message) => {
      if (message.role !== 'user' && message.role !== 'assistant') return;
      visibleCount += 1;
      appendMessage(
        message.role,
        message.content,
        message.cost,
        {
          ...(message.metadata || {}),
          message_id: message.id,
          history_position: message.history_position,
          is_favorite: Boolean(message.is_favorite),
        },
      );
    });
    refreshHistoryWindowPositions();
    if (visibleCount === 0) renderEmptyState();
    else renderOlderMessagesControl();
    renderLatestMessagesControl();
    updateChatHistoryProgress();
    return visibleCount;
  }

  function newSession(openChat = true) {
    releaseVoiceAudio();
    const id = crypto.randomUUID ? crypto.randomUUID() : Date.now().toString(36);
    leaveCurrentSession(id);
    cancelHistoryNavigation();
    state.currentSession = id;
    state.intimacyVitals = null;
    renderIntimacyVitals({ visible: false });
    state.oldestMessageId = 0;
    state.latestMessageId = 0;
    resetHistoryMetrics();
    state.loadedSessionId = id;
    restoreSessionDraft(id);
    resetContextBudgetView();
    // 新窗口在第一条消息发送前只存在浏览器内。明确标记后，后台轮询
    // 不会拿这个临时 UUID 请求 404，也不会在控制台制造假故障。
    state.sessions.unshift({
      id,
      title: '新对话',
      message_count: 0,
      total_cost: 0,
      persisted: false,
    });
    renderSessionList();
    renderEmptyState();
    updateChatHistoryProgress();
    dom.headerTitle.textContent = '新对话';
    loadFileWorkspace().catch(() => {});
    if (openChat) switchView('chat');
  }

  async function switchSession(id, openChat = true) {
    releaseVoiceAudio();
    const changingSession = id !== state.currentSession;
    if (changingSession) leaveCurrentSession(id);
    cancelHistoryNavigation();
    const historyEpoch = state.historyNavigationEpoch;
    state.currentSession = id;
    state.intimacyVitals = null;
    renderIntimacyVitals({ visible: false });
    state.oldestMessageId = 0;
    state.latestMessageId = 0;
    resetHistoryMetrics();
    state.loadedSessionId = null;
    if (changingSession) restoreSessionDraft(id);
    else resetPresenceTracker();
    resetContextBudgetView();
    renderSessionList();
    if (openChat) switchView('chat');

    let session = state.sessions.find((s) => s.id === id);
    if (!session) {
      try {
        const response = await fetch(`/api/sessions/${encodeURIComponent(id)}`, { cache: 'no-store' });
        if (response.ok) session = { ...(await response.json()), persisted: true };
      } catch (_) {}
    }
    if (id !== state.currentSession) return false;
    dom.headerTitle.textContent = session?.title || companionName;

    // 浏览器刚创建的新窗口在第一条消息前还没有后端 session。它本来就
    // 是“最新且为空”，不应拿 message-page 去请求一个必然 404 的资源。
    if (session?.persisted === false) {
      dom.messages.innerHTML = '';
      state.loadedSessionId = id;
      renderEmptyState();
      updateChatHistoryProgress();
      await loadFileWorkspace();
      return true;
    }

    // 加载历史消息
    const controller = new AbortController();
    state.historyPageController = controller;
    try {
      const resp = await fetch(
        `/api/sessions/${encodeURIComponent(id)}/message-page?limit=${state.messagePageSize}`,
        { cache: 'no-store', signal: controller.signal },
      );
      const page = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(page.detail || '消息读取失败');
      if (id !== state.currentSession || historyEpoch !== state.historyNavigationEpoch) return false;
      renderHistoryMessagePage(page);
      scrollToBottom({
        instant: true,
        expectedSession: id,
        expectedHistoryEpoch: historyEpoch,
      });
      requestAnimationFrame(updateChatHistoryProgress);
      await loadFileWorkspace();
      loadIntimacyVitals().catch(() => {});
      loadCoPresenceData().catch(() => {});
      return true;
    } catch (e) {
      if (
        id !== state.currentSession
        || historyEpoch !== state.historyNavigationEpoch
        || e?.name === 'AbortError'
      ) return false;
      console.error('加载消息失败:', e);
      state.loadedSessionId = null;
      const detail = String(e?.message || '消息读取失败');
      dom.headerStatus.textContent = `消息读取失败：${detail}`;
      return false;
    } finally {
      if (state.historyPageController === controller) {
        state.historyPageController = null;
      }
    }
  }

  function resetContextBudgetView() {
    state.lastContextBudget = null;
    state.lastContextUsage = null;
    state.contextCompression = null;
    if (dom.contextBudgetPill) {
      dom.contextBudgetPill.textContent = '上下文待命';
      dom.contextBudgetPill.className = 'context-budget-pill idle';
      dom.contextBudgetPill.title = '发送一条消息后显示本机上下文估算；点击可查看压缩状态。';
    }
    renderContextActualUsage();
  }

  function renderOlderMessagesControl() {
    dom.messages.querySelector('.older-messages-control:not(.latest-messages-control)')?.remove();
    if (!state.hasOlderMessages) return;
    const control = document.createElement('div');
    control.className = 'older-messages-control';
    control.innerHTML = '<button type="button">加载更早的消息</button>';
    control.querySelector('button')?.addEventListener('click', loadOlderMessages);
    dom.messages.prepend(control);
  }

  function renderLatestMessagesControl() {
    dom.messages.querySelector('.latest-messages-control')?.remove();
    if (state.historyWindowAtLatest) return;
    const control = document.createElement('div');
    control.className = 'older-messages-control latest-messages-control';
    control.innerHTML = '<button type="button">返回最新消息</button>';
    control.querySelector('button')?.addEventListener('click', async () => {
      if (!state.currentSession) return;
      await switchSession(state.currentSession, false);
    });
    dom.messages.appendChild(control);
  }

  function trimHistoryDomFromBottom() {
    const messages = [...dom.messages.querySelectorAll('.message')];
    const overflow = Math.max(0, messages.length - state.messageDomLimit);
    if (!overflow) return 0;
    messages.slice(-overflow).forEach((message) => message.remove());
    state.historyWindowAtLatest = false;
    return overflow;
  }

  function trimLatestHistoryDomFromTop(expectedSession = state.currentSession) {
    if (
      !dom.messages
      || !state.historyWindowAtLatest
      || expectedSession !== state.currentSession
    ) return 0;
    const messages = [...dom.messages.querySelectorAll('.message')];
    const overflow = Math.max(0, messages.length - state.messageDomLimit);
    if (!overflow) return 0;

    const wasNearBottom = (
      dom.messages.scrollHeight - dom.messages.scrollTop - dom.messages.clientHeight < 24
    );
    const anchor = messages[overflow] || null;
    const anchorTop = anchor?.getBoundingClientRect().top;
    messages.slice(0, overflow).forEach((message) => message.remove());

    refreshHistoryWindowPositions();
    const firstPersisted = dom.messages.querySelector('.message[data-message-id]');
    const firstMessageId = Number(firstPersisted?.dataset.messageId || 0);
    if (firstMessageId > 0) state.oldestMessageId = firstMessageId;
    const persistedCount = dom.messages.querySelectorAll('.message[data-message-id]').length;
    const previouslyHadOlder = state.hasOlderMessages;
    state.hasOlderMessages = state.historyStartPosition > 1
      || state.totalMessageCount > persistedCount;
    const hasOlderControl = Boolean(
      dom.messages.querySelector('.older-messages-control:not(.latest-messages-control)'),
    );
    if (previouslyHadOlder !== state.hasOlderMessages
        || (state.hasOlderMessages && !hasOlderControl)) {
      renderOlderMessagesControl();
    }
    updateChatHistoryProgress();

    if (wasNearBottom) {
      dom.messages.scrollTop = dom.messages.scrollHeight;
    } else if (anchor?.isConnected && Number.isFinite(anchorTop)) {
      dom.messages.scrollTop += anchor.getBoundingClientRect().top - anchorTop;
    }
    return overflow;
  }

  async function loadOlderMessages(options = {}) {
    const activeSession = options.expectedSession || state.currentSession;
    const activeEpoch = Number.isInteger(options.expectedEpoch)
      ? options.expectedEpoch
      : state.historyNavigationEpoch;
    if (!activeSession || activeSession !== state.currentSession) {
      return { ok: false, reason: 'session-changed' };
    }
    if (!state.hasOlderMessages) return { ok: false, reason: 'at-start' };
    if (state.loadingOlderMessages) return { ok: false, reason: 'busy' };
    if (state.isStreaming) return { ok: false, reason: 'streaming' };
    state.loadingOlderMessages = true;
    const button = dom.messages.querySelector('.older-messages-control button');
    if (button) { button.disabled = true; button.textContent = '正在读取更早消息…'; }
    const previousScrollTop = dom.messages.scrollTop;
    const previousScrollBehavior = dom.messages.style.scrollBehavior;
    const previousOverflowAnchor = dom.messages.style.overflowAnchor;
    const anchor = dom.messages.querySelector('.message');
    const anchorTop = anchor?.getBoundingClientRect().top;
    dom.messages.style.scrollBehavior = 'auto';
    dom.messages.style.overflowAnchor = 'none';
    const previousOldestId = Number(state.oldestMessageId || 0);
    const controller = new AbortController();
    const renderToken = ++state.historyRenderToken;
    state.historyPageController = controller;
    const isCurrent = () => (
      activeSession === state.currentSession
      && activeEpoch === state.historyNavigationEpoch
      && state.historyPageController === controller
    );
    try {
      const response = await fetch(
        `/api/sessions/${encodeURIComponent(activeSession)}/message-page?limit=${state.messagePageSize}&before_id=${previousOldestId}`,
        { cache: 'no-store', signal: controller.signal },
      );
      const page = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(page.detail || '更早消息读取失败');
      if (!isCurrent()) return { ok: false, reason: 'session-changed' };
      const pageItems = Array.isArray(page.items) ? page.items : [];
      const nextOldestId = Number(page.oldest_id || 0);
      if (!pageItems.length || !nextOldestId || nextOldestId >= previousOldestId) {
        throw new Error('分页没有向更早历史推进，已停止读取');
      }
      const insertedMessages = [];
      pageItems.forEach((message) => {
        if (message.role === 'user' || message.role === 'assistant') {
          insertedMessages.push(appendMessage(
            message.role, message.content, message.cost,
            {
              ...(message.metadata || {}),
              message_id: message.id,
              history_position: message.history_position,
              is_favorite: Boolean(message.is_favorite),
            },
            anchor,
          ));
        }
      });
      state.totalMessageCount = Math.max(
        state.totalMessageCount,
        Number(page.total_count || 0),
      );
      state.oldestMessageId = nextOldestId;
      state.hasOlderMessages = Boolean(page.has_more);
      trimHistoryDomFromBottom();
      refreshHistoryWindowPositions();
      renderOlderMessagesControl();
      renderLatestMessagesControl();
      updateChatHistoryProgress();

      const restoreAnchor = () => {
        if (
          activeSession !== state.currentSession
          || activeEpoch !== state.historyNavigationEpoch
          || renderToken !== state.historyRenderToken
        ) return;
        if (anchor?.isConnected && Number.isFinite(anchorTop)) {
          dom.messages.scrollTop += anchor.getBoundingClientRect().top - anchorTop;
        } else {
          dom.messages.scrollTop = previousScrollTop;
        }
      };
      insertedMessages.forEach((messageEl) => {
        messageEl.querySelectorAll('img').forEach((image) => {
          if (!image.complete) {
            image.addEventListener(
              'load',
              () => requestAnimationFrame(restoreAnchor),
              { once: true },
            );
          }
        });
      });
      requestAnimationFrame(() => {
        restoreAnchor();
        requestAnimationFrame(() => {
          restoreAnchor();
          dom.messages.style.scrollBehavior = previousScrollBehavior;
          dom.messages.style.overflowAnchor = previousOverflowAnchor;
        });
      });
      return {
        ok: true,
        loaded: pageItems.length,
        hasMore: state.hasOlderMessages,
      };
    } catch (error) {
      dom.messages.style.scrollBehavior = previousScrollBehavior;
      dom.messages.style.overflowAnchor = previousOverflowAnchor;
      if (!isCurrent() || error?.name === 'AbortError') {
        return { ok: false, reason: 'session-changed' };
      }
      if (button) { button.disabled = false; button.textContent = `读取失败：${error.message}`; }
      return { ok: false, reason: 'error', error };
    } finally {
      if (state.historyPageController === controller) {
        state.historyPageController = null;
        state.loadingOlderMessages = false;
      }
    }
  }

  // 长按菜单
  let contextSessionId = null;

  function showContextMenu(e, sessionId) {
    contextSessionId = sessionId;
    const touch = e.touches ? e.touches[0] : e;
    dom.contextMenu.style.left = `${touch.clientX}px`;
    dom.contextMenu.style.top = `${touch.clientY}px`;
    dom.contextMenu.classList.remove('hidden');
  }

  async function handleContextMenu(e) {
    const action = e.target.dataset.action;
    if (!action || !contextSessionId) return;

    if (action === 'rename') {
      const newName = prompt('重命名对话:');
      if (newName) {
        fetch(`/api/sessions/${contextSessionId}/rename`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ title: newName }),
        }).then(() => {
          const s = state.sessions.find((s) => s.id === contextSessionId);
          if (s) s.title = newName;
          renderSessionList();
          if (state.currentSession === contextSessionId) {
            dom.headerTitle.textContent = newName;
          }
        });
      }
    }

    if (action === 'delete') {
      const target = contextSessionId;
      if (!confirm('删除这段会话吗？完整聊天原文与本会话统计会删除；已经形成的长期记忆会保留，并只留下创建时的来源摘录用于核对。')) {
        dom.contextMenu.classList.add('hidden');
        return;
      }
      try {
        const resp = await fetch(`/api/sessions/${encodeURIComponent(target)}`, { method: 'DELETE' });
        if (!resp.ok) {
          const data = await resp.json().catch(() => ({}));
          throw new Error(data.detail || '删除失败');
        }
        state.sessions = state.sessions.filter((session) => session.id !== target);
        if (state.currentSession === target) {
          if (state.sessions.length) await switchSession(state.sessions[0].id);
          else newSession();
        } else {
          renderSessionList();
        }
      } catch (error) {
        alert(`会话删除失败：${error.message}`);
      }
    }

    dom.contextMenu.classList.add('hidden');
  }

  // ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  //  聊天 & SSE 流式
  // ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  function schedulePendingRequestCheck(delay = 2500) {
    clearTimeout(state.reliabilityPollTimer);
    if (document.hidden) return;
    state.reliabilityPollTimer = setTimeout(
      () => reconcilePendingChatRequest({ poll: true }),
      Math.max(1000, Number(delay || 0)),
    );
  }

  async function reconcilePendingChatRequest(reason = {}) {
    const reliability = window.ChatReliability;
    const pending = reliability?.pending?.();
    if (!reliability || !pending?.client_request_id) return;
    try {
      const result = await reliability.reconcile();
      if (result.status === 'processing') {
        state.recoveringPendingRequest = !state.isStreaming;
        if (!state.isStreaming) {
          dom.headerStatus.textContent = '上一条消息仍在服务器生成中…';
          updateSendState();
        }
        schedulePendingRequestCheck();
        return;
      }

      if (reliability.isTerminal(result.status)) {
        clearTimeout(state.reliabilityPollTimer);
        state.recoveringPendingRequest = false;
        state.recoveryDraftRequestId = null;
        if (
          state.isStreaming
          && state.activeClientRequestId === pending.client_request_id
          && state.activeChatController
        ) {
          state.recoveredRequest = result;
          state.activeChatController.abort();
          return;
        }
        if (result.session_id && result.session_id === state.currentSession) {
          await switchSession(result.session_id, false);
        } else {
          await loadSessions();
        }
        dom.headerStatus.textContent = result.status === 'completed'
          ? '已恢复上一条回复'
          : (result.status === 'blocked' ? '原草稿被关系诚实保护拦截' : '已恢复中断状态');
        updateSendState();
        return;
      }

      if (result.status === 'missing') {
        const age = Date.now() - Number(pending.created_at_ms || 0);
        if (age < 5000) {
          schedulePendingRequestCheck(1500);
          return;
        }
        const payload = pending.payload;
        state.recoveringPendingRequest = false;
        if (payload && !pending.acknowledged && !state.isStreaming) {
          if (payload.session_id && payload.session_id !== state.currentSession) {
            state.currentSession = payload.session_id;
            if (!state.sessions.some((item) => item.id === payload.session_id)) {
              state.sessions.unshift({
                id: payload.session_id,
                title: '待恢复消息',
                message_count: 0,
                total_cost: 0,
                persisted: false,
              });
            }
            renderSessionList();
            renderEmptyState();
            dom.headerTitle.textContent = '待恢复消息';
          }
          dom.input.value = String(payload.message || '');
          state.pendingAttachments = Array.isArray(payload.attachment_items)
            ? payload.attachment_items : [];
          state.recoveryDraftRequestId = pending.client_request_id;
          autoResize(dom.input);
          renderAttachmentTray();
          dom.headerStatus.textContent = '上一条没有送达，已放回输入框';
          updateSendState();
        } else if (pending.acknowledged) {
          reliability.clear(pending.client_request_id);
          dom.headerStatus.textContent = '请求记录已失效，请查看会话历史';
          updateSendState();
        }
      }
    } catch (error) {
      if (reason.foreground || reason.startup) {
        dom.headerStatus.textContent = '网络恢复后会继续核对上一条消息';
      }
      schedulePendingRequestCheck(4000);
    }
  }

  function stopGeneration() {
    const reliability = window.ChatReliability;
    const pendingRequestId = reliability?.pending?.()?.client_request_id || '';
    const requestId = state.activeClientRequestId || pendingRequestId;
    if ((!state.isStreaming && !state.recoveringPendingRequest) || !requestId) return;
    state.stopRequested = true;
    dom.headerStatus.textContent = '正在停止生成…';
    reliability?.cancel?.(requestId)?.finally?.(() => {
      schedulePendingRequestCheck(500);
    });
    // Aborting the local SSE is only a transport action. The explicit cancel
    // request above is what tells the detached server producer to stop.
    state.activeChatController?.abort();
    updateSendState();
  }

  async function sendMessage(overrides = {}) {
    const hasTextOverride = Object.prototype.hasOwnProperty.call(overrides || {}, 'text');
    const hasAttachmentOverride = Object.prototype.hasOwnProperty.call(overrides || {}, 'attachments');
    const text = String(hasTextOverride ? overrides.text : dom.input.value).trim();
    const attachments = hasAttachmentOverride
      ? [...(Array.isArray(overrides.attachments) ? overrides.attachments : [])]
      : [...state.pendingAttachments];
    const blockedAudio = attachments.find((item) => (
      item?.kind === 'audio' && item.analysis_status !== 'ready'
    ));
    if (blockedAudio) {
      alert(blockedAudio.analysis_status === 'error'
        ? '这段音频没有被听海听完。请先点“重新分析”，成功后再发送。'
        : '听海还在准备或分析这段音频。显示“已听完”后才能发送。');
      startOceanPolling();
      return;
    }
    if (
      (!text && attachments.length === 0)
      || state.isStreaming
      || state.recoveringPendingRequest
      || state.uploadingAttachments > 0
    ) return;
    if (!state.currentSession) newSession(false);
    if (
      state.loadedSessionId !== state.currentSession
      || !state.historyWindowAtLatest
    ) {
      const restored = await switchSession(state.currentSession, false);
      if (!restored) {
        // switchSession 已显示真正的读取错误。保留输入内容，不伪装成 Key
        // 或发送失败；历史恢复成功后用户可以直接再次发送。
        return;
      }
    }
    const expressionRhythm = hasTextOverride
      ? {
          active_ms: 0,
          revision_count: 0,
          pause_count: 0,
          clear_count: 0,
          paste_count: 0,
          input_method: overrides.voiceTranscript ? 'voice' : 'unknown',
          draft_active: false,
        }
      : finishPresenceForSubmit(overrides.voiceTranscript ? 'voice' : '');

    dom.input.value = '';
    state.pendingAttachments = [];
    renderAttachmentTray();
    updateSendState();
    autoResize(dom.input);
    const userEl = appendMessage('user', text, null, {
      display_text: text,
      message_type: overrides.voiceTranscript
        ? 'voice'
        : (text && attachments.length ? 'mixed' : (attachments.length ? 'file' : 'text')),
      voice_transcript: overrides.voiceTranscript === true,
      voice_duration_ms: Number(overrides.voiceDurationMs || 0),
      voice_transcriber: String(overrides.voiceTranscriber || ''),
      attachments,
    });
    state.activeUserMessageEl = userEl;
    scrollToBottom();

    const typingEl = appendTyping();
    let typingVisible = true;
    state.isStreaming = true;
    dom.headerStatus.textContent = '思考中...';
    if (dom.contextBudgetPill) {
      dom.contextBudgetPill.textContent = '正在估算上下文…';
      dom.contextBudgetPill.className = 'context-budget-pill busy';
    }

    const requestSessionId = state.currentSession;
    const clientMetadata = {
      expression_rhythm: expressionRhythm,
    };
    if (overrides.voiceTranscript) {
      clientMetadata.voice_transcript = true;
      clientMetadata.voice_duration_ms = Number(overrides.voiceDurationMs || 0);
      clientMetadata.voice_transcriber = String(
        overrides.voiceTranscriber || 'browser'
      );
      clientMetadata.voice_acoustic = overrides.voiceAcoustic || {};
      clientMetadata.voice_mood = overrides.voiceMood || {};
      clientMetadata.voice_private_mode = overrides.voicePrivateMode === true;
      clientMetadata.voice_sleep_mode = overrides.voiceSleepMode === true;
    }
    const reliability = window.ChatReliability;
    const previousPending = reliability?.pending?.();
    const reuseRecoveredId = Boolean(
      state.recoveryDraftRequestId
      && previousPending?.client_request_id === state.recoveryDraftRequestId
      && previousPending?.session_id === requestSessionId
      && String(previousPending?.payload?.message || '') === text
    );
    const clientRequestId = reuseRecoveredId
      ? state.recoveryDraftRequestId
      : (reliability?.createId?.() || `${Date.now().toString(36)}${Math.random().toString(36).slice(2)}`);
    if (previousPending && !reuseRecoveredId) {
      reliability?.clear?.(previousPending.client_request_id);
    }
    const wirePayload = {
      message: text,
      session_id: requestSessionId,
      provider: state.activeProvider,
      model: state.activeModel,
      options: getBrainOptionsForRequest(),
      attachments: attachments.map((item) => item.id),
      client_metadata: clientMetadata,
      client_request_id: clientRequestId,
    };
    try {
      reliability?.begin?.({
        ...wirePayload,
        attachment_items: attachments.map((item) => ({
          id: item.id,
          name: item.name,
          kind: item.kind,
          size: item.size,
          preview_url: item.preview_url,
          status: item.status,
          analysis_status: item.analysis_status,
          analysis_stage: item.analysis_stage,
          analysis_progress: item.analysis_progress,
          analysis_error: item.analysis_error,
        })),
      }, clientRequestId);
    } catch (error) {
      console.warn('无法持久保存待恢复请求:', error);
    }
    state.recoveryDraftRequestId = null;
    const controller = new AbortController();
    state.activeChatController = controller;
    state.activeRequestId = null;
    state.activeClientRequestId = clientRequestId;
    state.stopRequested = false;
    updateSendState();
    let timeoutTriggered = false;
    let aiEl = null;
    // v6.5.2：空闲看门狗——只有连续 120 秒完全没有收到任何数据才算超时。
    // 深度思考模式下超过 5 分钟的长回复不会再被“总时长”腰斩。
    let timeoutId = null;
    let aiText = '';
    const IDLE_TIMEOUT_MS = 120000;
    const armWatchdog = () => {
      clearTimeout(timeoutId);
      timeoutId = setTimeout(() => {
        timeoutTriggered = true;
        controller.abort();
      }, IDLE_TIMEOUT_MS);
    };
    armWatchdog();

    try {
      const resp = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        signal: controller.signal,
        body: JSON.stringify(wirePayload),
      });

      if (!resp.ok) {
        const raw = await resp.text();
        let detail = raw;
        try {
          const parsed = JSON.parse(raw);
          detail = parsed.error || parsed.detail || raw;
        } catch (_) {}
        throw new Error(`HTTP ${resp.status}: ${detail}`);
      }
      if (!resp.body) throw new Error('浏览器没有收到流式响应体');

      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let finished = false;
      let requestId = null;
      let finalTrace = null;
      let finalIntegrity = null;
      let finalRequestState = null;
      let replayedRequest = false;

      if (typingVisible) {
        typingEl.remove();
        typingVisible = false;
      }

      const handleEventBlock = (block) => {
        const dataLines = block.split(/\r?\n/)
          .filter((line) => line.startsWith('data:'))
          .map((line) => line.slice(5).trimStart());
        if (!dataLines.length) return;

        const data = dataLines.join('\n');
        if (data === '[DONE]') {
          finished = true;
          return;
        }

        let event;
        try {
          event = JSON.parse(data);
        } catch (e) {
          console.warn('SSE JSON 未完整，已跳过:', data);
          return;
        }

        if (event.type === 'request') {
          requestId = event.request_id || requestId;
          state.activeRequestId = requestId;
          state.activeClientRequestId = event.client_request_id || state.activeClientRequestId;
          if (event.provider) {
            state.activeProvider = event.provider;
            state.activeModel = event.model || state.activeModel;
            localStorage.setItem('companion:provider', state.activeProvider);
            updateModelPill();
            loadProviders(false).catch(() => {});
          }
          replayedRequest = event.replayed === true;
          reliability?.acknowledge?.(state.activeClientRequestId, {
            trace_id: requestId,
            user_message_id: event.user_message_id,
          });
          if (event.user_message_id && userEl) {
            setMessageIdentity(userEl, event.user_message_id);
          }
          dom.headerStatus.textContent = requestId ? `请求 ${requestId}` : '处理中…';
        } else if (event.type === 'recovery') {
          dom.headerStatus.textContent = event.status === 'processing'
            ? '请求已接收，正在等待服务器完成…'
            : '正在恢复上一条回复…';
        } else if (event.type === 'tool') {
          const ok = event.ok !== false;
          dom.headerStatus.textContent = `${ok ? '已读取' : '工具失败'}：${event.name || '系统工具'}`;
        } else if (event.type === 'trace' && event.trace) {
          finalTrace = event.trace;
          requestId = event.trace.id || requestId;
          if (aiEl && event.trace.status !== 'running') attachTrace(aiEl, event.trace);
        } else if (event.type === 'context_budget' && event.budget) {
          renderContextBudget(event.budget);
        } else if (event.type === 'intimacy_vitals' && event.vitals) {
          if (requestSessionId === state.currentSession) {
            if (event.vitals.visible && !aiEl) aiEl = appendMessage('assistant', '');
            renderIntimacyVitals(event.vitals, aiEl);
            if (event.vitals.visible) scrollToBottom();
          }
        } else if (event.type === 'state_changes' && Array.isArray(event.changes) && event.changes.length) {
          if (!aiEl) aiEl = appendMessage('assistant', '');
          appendStateChanges(aiEl, event.changes);
          scrollToBottom();
        } else if (event.type === 'inner_os' && event.item) {
          if (!aiEl) aiEl = appendMessage('assistant', '');
          appendInnerOS(aiEl, event.item);
          scrollToBottom();
        } else if (event.type === 'thinking') {
          if (!aiEl) aiEl = appendMessage('assistant', '');
          appendThinkingDelta(aiEl, event.text || '', {
            label: event.label,
            provider: event.provider,
          });
          scrollToBottom();
        } else if (event.type === 'text') {
          aiText += event.text || '';
          if (!aiEl) {
            aiEl = appendMessage('assistant', aiText);
          } else {
            setMessageText(aiEl, aiText);
          }
          scrollToBottom();
        } else if (event.type === 'replace_text') {
          if (!aiEl) aiEl = appendMessage('assistant', event.text || '');
          setMessageText(aiEl, event.text || '');
          aiText = event.text || '';
        } else if (event.type === 'sticker' && event.sticker) {
          if (!aiEl) aiEl = appendMessage('assistant', aiText || '');
          appendStickerToMessage(aiEl, event.sticker);
          scrollToBottom();
        } else if (event.type === 'status') {
          dom.headerStatus.textContent = event.label || '处理中…';
        } else if (event.type === 'relational_honesty') {
          if (event.action === 'rewritten') {
            dom.headerStatus.textContent = '关系诚实检查已重写原草稿';
          } else if (event.action === 'blocked') {
            dom.headerStatus.textContent = '原草稿未通过关系诚实检查';
          }
        } else if (event.type === 'usage' && event.usage) {
          const u = event.usage;
          state.lastContextUsage = { ...u, request_id: event.request_id || requestId || '' };
          renderContextActualUsage();
          const meta = aiEl?.querySelector('.msg-meta');
          if (meta) {
            const input = Number(u.input_tokens || 0);
            const output = Number(u.output_tokens || 0);
            const cost = Number(u.cost || 0);
            meta.textContent = `${input + output} tokens · $${cost.toFixed(4)}`;
            if (Number(u.reasoning_tokens || 0) > 0) {
              meta.textContent += ` · 思考${u.reasoning_tokens}t`;
            }
            if (Number(u.cache_read || 0) > 0) {
              meta.textContent += ` · 缓存${u.cache_read}t`;
            }
            if (u.native_protocol) {
              meta.title = `协议: ${u.native_protocol}`;
            }
          }
        } else if (event.type === 'brain_integrity' && event.report) {
          finalIntegrity = event.report;
          if (aiEl) attachIntegrity(aiEl, finalIntegrity);
        } else if (event.type === 'message_saved' && event.message_id && aiEl) {
          setMessageIdentity(aiEl, event.message_id);
        } else if (event.type === 'request_state') {
          finalRequestState = event;
          if (reliability?.isTerminal?.(event.status)) {
            reliability.clear(event.client_request_id || state.activeClientRequestId);
            if (replayedRequest) state.recoveredRequest = event;
          } else if (event.status === 'processing') {
            schedulePendingRequestCheck();
          }
        } else if (event.type === 'error') {
          appendMessage('assistant', `⚠️ ${event.error || '上游接口出错'}${event.request_id ? `\n请求编号：${event.request_id}` : ''}`);
        }
      };

      while (!finished) {
        const { done, value } = await reader.read();
        if (done) break;
        armWatchdog();
        buffer += decoder.decode(value, { stream: true });

        let boundary;
        while ((boundary = buffer.search(/\r?\n\r?\n/)) !== -1) {
          const block = buffer.slice(0, boundary);
          const sep = buffer.slice(boundary).match(/^\r?\n\r?\n/)[0];
          buffer = buffer.slice(boundary + sep.length);
          handleEventBlock(block);
          if (finished) break;
        }
      }

      buffer += decoder.decode();
      if (buffer.trim()) handleEventBlock(buffer.trim());
      if (aiEl && finalIntegrity) attachIntegrity(aiEl, finalIntegrity);
      if (aiEl && finalTrace) attachTrace(aiEl, finalTrace);
      if (aiEl && aiText && !state.stopRequested) {
        window.JTYSFX?.final?.(aiEl, aiText)?.catch?.((error) => {
          console.warn('本地环境音没有完成:', error);
        });
      }
      if (
        aiEl
        && state.voice?.ready
        && state.voice?.settings?.auto_play
        && !state.stopRequested
        && !overrides.suppressAutoVoice
      ) {
        speakMessage(aiEl, { silent: true, autoplay: true }).catch((error) => {
          console.warn('自动朗读没有完成:', error);
        });
      }
    } catch (e) {
      if (typingVisible) {
        typingEl.remove();
        typingVisible = false;
      }
      if (e.name === 'AbortError' && state.recoveredRequest) {
        dom.headerStatus.textContent = '服务器已完成，正在恢复回复…';
      } else if (e.name === 'AbortError' && state.stopRequested) {
        if (aiEl) {
          aiEl.classList.add('interrupted');
          const meta = aiEl.querySelector('.msg-meta');
          if (meta) meta.textContent = `${meta.textContent ? `${meta.textContent} · ` : ''}已停止生成`;
          reclaimInterruptedMessage(aiEl, requestSessionId, userEl?.dataset.messageId)
            .catch(() => {});
        }
        schedulePendingRequestCheck(800);
      } else {
        const msg = e.name === 'AbortError' && timeoutTriggered
          ? '连接暂时中断，正在向服务器核对这条消息。'
          : `连接中断：${e.message}。正在核对是否已经保存。`;
        appendMessage('assistant', `⚠️ ${msg}`);
        schedulePendingRequestCheck(800);
      }
      const pendingReceipt = reliability?.pending?.();
      if (
        !state.stopRequested
        && !pendingReceipt?.acknowledged
        && attachments.length
        && state.pendingAttachments.length === 0
      ) {
        state.pendingAttachments = attachments;
        renderAttachmentTray();
      }
    } finally {
      const recovered = state.recoveredRequest;
      clearTimeout(timeoutId);
      if (typingVisible) typingEl.remove();
      state.isStreaming = false;
      state.activeChatController = null;
      state.activeRequestId = null;
      state.activeClientRequestId = null;
      state.activeUserMessageEl = null;
      state.stopRequested = false;
      state.recoveredRequest = null;
      dom.headerStatus.textContent = '';
      updateSendState();
      loadSessions();
      loadFileWorkspace().catch(() => {});
      loadCoreData().catch(() => {});
      loadInnerStateData().catch(() => {});
      loadIntimacyVitals().catch(() => {});
      loadCoPresenceData().catch(() => {});
      if (recovered?.session_id) {
        await switchSession(recovered.session_id, false);
        dom.headerStatus.textContent = recovered.status === 'completed'
          ? '已恢复上一条回复'
          : '已恢复上一条请求的最终状态';
      } else if (reliability?.pending?.()) {
        schedulePendingRequestCheck(1000);
      }
    }
    return { messageEl: aiEl, text: aiText, sessionId: requestSessionId };
  }

  function appendMessage(role, content, cost, metadata = {}, beforeNode = null) {
    removeEmptyState();
    const el = document.createElement('div');
    el.className = `message ${role}`;
    const displayText = Object.prototype.hasOwnProperty.call(metadata || {}, 'display_text')
      ? String(metadata.display_text || '')
      : String(content || '');
    el.dataset.role = role;
    if (role === 'assistant') el.dataset.companionInitial = companionInitial;
    el._rawText = displayText;
    el._messageMetadata = metadata || {};
    if (metadata?.voice_transcript) el.dataset.voiceTranscript = 'true';
    setMessageHistoryPosition(el, metadata?.history_position);
    el.innerHTML = `
      <div class="message-state-host"></div>
      <div class="message-thinking-host"></div>
      <div class="bubble${displayText ? '' : ' hidden'}"></div>
      <div class="message-media"></div>
      <div class="message-footer">
        <div class="msg-meta">${cost ? `$${Number(cost).toFixed(4)}` : ''}${metadata?.interrupted ? `${cost ? ' · ' : ''}已停止生成` : ''}</div>
        <div class="message-actions">
          ${role === 'user'
            ? '<button type="button" data-message-action="edit" disabled>编辑并分支</button>'
            : '<button type="button" data-message-action="speak">朗读</button><button type="button" data-message-action="retry">重新回答</button>'}
          <button type="button" data-message-action="favorite" class="${metadata?.is_favorite ? 'is-favorite' : ''}" aria-pressed="${metadata?.is_favorite ? 'true' : 'false'}">${metadata?.is_favorite ? '★ 已收藏' : '☆ 收藏'}</button>
        </div>
      </div>
    `;
    if (beforeNode) dom.messages.insertBefore(el, beforeNode);
    else dom.messages.appendChild(el);
    setMessageText(el, displayText);
    if (metadata?.message_id) setMessageIdentity(el, metadata.message_id);
    const attachments = Array.isArray(metadata?.attachments) ? metadata.attachments : [];
    attachments.forEach((item) => appendAttachmentToMessage(el, item));
    if (metadata?.sticker) appendStickerToMessage(el, metadata.sticker);
    if (metadata?.brain_integrity) attachIntegrity(el, metadata.brain_integrity);
    if (role === 'assistant' && metadata?.message_id) {
      // Existing/history messages get a local SFX replay button when relevant;
      // autoplay is reserved for the newly completed visible reply.
      window.JTYSFX?.decorate?.(el, displayText);
    }
    if (role === 'assistant' && metadata?.thinking_available) {
      ensureThinkingPanel(el, {
        messageId: metadata.message_id,
        label: metadata.thinking_label || '模型 API 可见推理',
        provider: metadata.thinking_provider,
        chars: Number(metadata.thinking_chars || 0),
        open: false,
      });
    }
    if (!beforeNode && state.historyWindowAtLatest) {
      trimLatestHistoryDomFromTop(state.currentSession);
    }
    return el;
  }

  function appendStateChanges(messageEl, changes = []) {
    const host = messageEl?.querySelector('.message-state-host');
    if (!host || !Array.isArray(changes)) return;
    if (state.innerState?.settings?.visible_changes === false) return;
    const limit = Math.max(1, Math.min(
      4,
      Number(state.innerState?.settings?.max_visible_changes || 3),
    ));
    const remaining = Math.max(
      0,
      limit - host.querySelectorAll('.message-state-chip').length,
    );
    changes.slice(0, remaining).forEach((item) => {
      const key = `${item.domain || ''}:${item.dimension || ''}:${item.after || ''}`;
      if (host.querySelector(`[data-state-key="${cssEscape(key)}"]`)) return;
      const chip = document.createElement('span');
      chip.className = `message-state-chip state-${item.domain || 'inner'} ${Number(item.delta || 0) < 0 ? 'down' : 'up'}`;
      chip.dataset.stateKey = key;
      const direction = Number(item.delta || 0) < 0 ? '↓' : '↑';
      chip.innerHTML = `<i>${direction}</i><strong>${esc(item.label || '状态')}</strong><em>${Number(item.after || 1)}/10</em>`;
      chip.title = `${item.before || 1}/10 → ${item.after || 1}/10 · ${item.description || item.reason || ''}`;
      host.appendChild(chip);
    });
  }

  function appendInnerOS(messageEl, item = {}) {
    const host = messageEl?.querySelector('.message-state-host');
    const content = String(item.content || '').trim();
    if (!host || !content) return;
    const key = String(item.id || `${item.source || 'turn'}:${content}`);
    if ([...host.querySelectorAll('.message-inner-os')].some((node) => node.dataset.osKey === key)) return;
    const note = document.createElement('button');
    note.type = 'button';
    note.className = 'message-inner-os';
    note.dataset.osKey = key;
    note.dataset.open = 'false';
    note.innerHTML = `
      <span>内心 OS${item.dominant_emotion ? ` · ${esc(item.dominant_emotion)}` : ''}</span>
      <strong>${esc(content)}</strong>
      <small>主观余波 · 不是事实记忆</small>`;
    note.addEventListener('click', () => {
      note.dataset.open = note.dataset.open === 'true' ? 'false' : 'true';
    });
    host.appendChild(note);
  }

  function setMessageIdentity(messageEl, messageId) {
    if (!messageEl || !messageId) return;
    messageEl.dataset.messageId = String(messageId);
    state.latestMessageId = Math.max(
      Number(state.latestMessageId || 0),
      Number(messageId || 0),
    );
    messageEl.querySelectorAll('[data-message-action]').forEach((button) => {
      button.disabled = false;
    });
  }

  async function reclaimInterruptedMessage(aiEl, sessionId, userMessageId) {
    // v6.5.2：点“停止生成”后，服务端仍会把半截回复存入本机数据库，
    // 但 message_saved 事件已随连接一起中断。这里静默取回真实的
    // message_id 与落库文本，让编辑 / 重答 / 朗读不用刷新就能用。
    if (!aiEl || !sessionId) return;
    for (let attempt = 0; attempt < 3; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 600 + attempt * 400));
      if (aiEl.dataset.messageId) return;
      try {
        const resp = await fetch(
          `/api/sessions/${encodeURIComponent(sessionId)}/message-page?limit=6`,
          { cache: 'no-store' },
        );
        if (!resp.ok) continue;
        const data = await resp.json();
        const items = Array.isArray(data.items) ? data.items : [];
        const saved = items.find((item) => (
          item.role === 'assistant'
          && (!userMessageId || Number(item.id) > Number(userMessageId))
        ));
        if (!saved) continue;
        setMessageIdentity(aiEl, saved.id);
        if (typeof saved.content === 'string' && saved.content) {
          setMessageText(aiEl, saved.content);
        }
        return;
      } catch (_) { /* 静默重试，最多三次 */ }
    }
  }

  function handleMessageAction(event) {
    const button = event.target.closest('[data-message-action]');
    if (!button) return;
    const messageEl = button.closest('.message');
    if (!messageEl || button.disabled) return;
    const action = button.dataset.messageAction;
    if (action === 'edit') startMessageEditor(messageEl);
    if (action === 'speak') speakMessage(messageEl);
    if (action === 'favorite') toggleMessageFavorite(messageEl, button);
    if (action === 'retry') {
      let previous = messageEl.previousElementSibling;
      while (previous && !previous.classList.contains('user')) previous = previous.previousElementSibling;
      if (previous) branchAndSend(previous, previous._rawText || '');
    }
  }

  async function toggleMessageFavorite(messageEl, button) {
    const messageId = Number(messageEl?.dataset.messageId || 0);
    if (!messageId || !button) return;
    const active = button.getAttribute('aria-pressed') === 'true';
    button.disabled = true;
    try {
      const response = await fetch(`/api/messages/${messageId}/favorite`, {
        method: active ? 'DELETE' : 'POST',
        headers: active ? undefined : { 'Content-Type': 'application/json' },
        body: active ? undefined : '{}',
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || '收藏操作失败');
      const next = !active;
      button.setAttribute('aria-pressed', next ? 'true' : 'false');
      button.classList.toggle('is-favorite', next);
      button.textContent = next ? '★ 已收藏' : '☆ 收藏';
      messageEl._messageMetadata = { ...(messageEl._messageMetadata || {}), is_favorite: next };
      if (dom.favoriteResults && !next) loadFavoriteMessages().catch(() => {});
    } catch (error) {
      alert(error.message || '收藏操作失败');
    } finally {
      button.disabled = false;
    }
  }

  function startMessageEditor(messageEl) {
    if (state.isStreaming) return alert('请先停止当前回复，再编辑旧消息。');
    if (!messageEl?.dataset.messageId || messageEl.querySelector('.message-editor')) return;
    dom.messages.querySelectorAll('.message-editor').forEach((editor) => editor.remove());
    const bubble = messageEl.querySelector('.bubble');
    const editor = document.createElement('div');
    editor.className = 'message-editor';
    editor.innerHTML = `
      <textarea rows="3" aria-label="编辑消息"></textarea>
      <div><button type="button" data-edit-cancel>取消</button><button type="button" class="primary" data-edit-submit>从这里重答</button></div>`;
    const textarea = editor.querySelector('textarea');
    textarea.value = messageEl._rawText || '';
    bubble?.classList.add('editing-hidden');
    bubble?.after(editor);
    textarea.focus();
    textarea.setSelectionRange(textarea.value.length, textarea.value.length);
    editor.querySelector('[data-edit-cancel]')?.addEventListener('click', () => {
      editor.remove();
      bubble?.classList.remove('editing-hidden');
    });
    editor.querySelector('[data-edit-submit]')?.addEventListener('click', async () => {
      const nextText = textarea.value.trim();
      if (!nextText) return textarea.focus();
      editor.querySelectorAll('button').forEach((button) => { button.disabled = true; });
      await branchAndSend(messageEl, nextText);
    });
  }

  async function branchAndSend(messageEl, nextText) {
    const messageId = messageEl?.dataset.messageId;
    if (!nextText || state.isStreaming) return;
    if (!messageId) {
      alert('这条消息还在保存中；如果刚刚中止了回复，刷新窗口后就可以从这里重答。');
      return;
    }
    try {
      const response = await fetch(`/api/messages/${encodeURIComponent(messageId)}/branch`, { method: 'POST' });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || data.error || '无法建立对话分支');
      await loadSessions();
      await switchSession(data.session_id, true);
      const attachments = Array.isArray(messageEl._messageMetadata?.attachments)
        ? messageEl._messageMetadata.attachments.filter((item) => item?.id)
        : [];
      await sendMessage({ text: nextText, attachments });
    } catch (error) {
      alert(`编辑消息失败：${error.message}`);
      messageEl.querySelectorAll('.message-editor button').forEach((button) => { button.disabled = false; });
    }
  }

  function renderContextBudget(budget) {
    if (!dom.contextBudgetPill || !budget) return;
    state.lastContextBudget = budget;
    if (budget.compression && typeof budget.compression === 'object') {
      state.contextCompression = { ...(state.contextCompression || {}), ...budget.compression };
    }
    const tokens = Number(budget.estimated_input_tokens || 0);
    const compact = tokens >= 1000 ? `${(tokens / 1000).toFixed(tokens >= 10000 ? 0 : 1)}k` : String(tokens);
    const compressedMessages = Number(budget.compression?.source_messages_compacted || 0);
    const history = budget.history || {};
    const boundedHistory = Number(history.dropped_messages || 0) + Number(history.truncated_messages || 0);
    dom.contextBudgetPill.textContent = boundedHistory
      ? `约 ${compact} 输入 · 近期原文已按上限收口`
      : compressedMessages
      ? `约 ${compact} 输入 · 已整理 ${compressedMessages.toLocaleString()} 条旧消息`
      : `约 ${compact} 输入 · ${budget.label || '已估算'}`;
    dom.contextBudgetPill.className = `context-budget-pill ${budget.level || 'good'}${compressedMessages ? ' has-compression' : ''}`;
    const part = budget.breakdown || {};
    const compression = budget.compression || {};
    dom.contextBudgetPill.title = [
      '点击打开上下文检查器。以下为本机字符估算，最终以 API usage 为准。',
      `系统规则：${Number(part.system || 0).toLocaleString()} 字`,
      `关系与记忆：${Number(part.dynamic_memory || 0).toLocaleString()} 字`,
      `压缩旧历史：${Number(part.compressed_history || 0).toLocaleString()} 字`,
      `可见对话：${Number(part.messages || 0).toLocaleString()} 字`,
      `本轮文件：${Number(part.explicit_files || 0).toLocaleString()} 字`,
      `常驻索引：${Number(part.pinned_files || 0).toLocaleString()} 字`,
      `按需摘取：${Number(part.retrieved_files || 0).toLocaleString()} 字`,
      `近期原文：${Number(history.selected_messages || 0).toLocaleString()} 条 / ${Number(history.selected_chars || 0).toLocaleString()} 字（上限 ${Number(history.max_chars || 0).toLocaleString()} 字）`,
      Number(history.dropped_messages || 0) || Number(history.truncated_messages || 0)
        ? `安全收口：停止向前 ${Number(history.dropped_messages || 0).toLocaleString()} 条；首尾保留 ${Number(history.truncated_messages || 0).toLocaleString()} 条超长消息`
        : '安全收口：本轮近期原文未触发字符上限',
      compression.enabled === false
        ? '自动整理：已为此窗口关闭'
        : `自动整理：${Number(compression.chapters || 0).toLocaleString()} 个章节 · 预计节省 ${Number(compression.estimated_saved_tokens || 0).toLocaleString()} tokens`,
    ].join('\n');
    renderContextReceipt(budget);
    if (dom.contextInspectorDialog?.open) renderContextInspector(state.contextCompression);
  }

  async function openContextInspector() {
    if (!state.currentSession || !dom.contextInspectorDialog) return;
    if (!dom.contextInspectorDialog.open) {
      if (dom.contextInspectorDialog.showModal) dom.contextInspectorDialog.showModal();
      else dom.contextInspectorDialog.setAttribute('open', '');
    }
    renderContextReceipt(state.lastContextBudget);
    renderContextActualUsage();
    if (dom.contextChapterList) dom.contextChapterList.innerHTML = '<div class="context-inspector-empty">正在读取本机压缩状态…</div>';
    if (dom.contextSourcePanel) {
      dom.contextSourcePanel.classList.add('hidden');
      dom.contextSourcePanel.innerHTML = '';
    }
    const activeSession = state.sessions.find((item) => item.id === state.currentSession);
    if (activeSession?.persisted === false) {
      const waiting = {
        enabled: true, unsaved: true, status: 'waiting', label: '发送第一条消息后开始记录',
        raw_message_limit: 500, chapters: 0, source_messages_compacted: 0,
        estimated_saved_tokens: 0, compression_ratio: 1, backlog_messages: 0,
        recent_chapters: [],
      };
      state.contextCompression = waiting;
      renderContextInspector(waiting);
      return;
    }
    try {
      const response = await fetch(`/api/context-compression/${encodeURIComponent(state.currentSession)}`, { cache: 'no-store' });
      const data = await response.json().catch(() => ({}));
      if (response.status === 404) {
        const waiting = {
          enabled: true, unsaved: true, status: 'waiting', label: '发送第一条消息后开始记录',
          raw_message_limit: 500, chapters: 0, source_messages_compacted: 0,
          estimated_saved_tokens: 0, compression_ratio: 1, backlog_messages: 0,
          recent_chapters: [],
        };
        state.contextCompression = waiting;
        renderContextInspector(waiting);
        return;
      }
      if (!response.ok) throw new Error(data.detail || data.error || '读取失败');
      state.contextCompression = data;
      renderContextInspector(data);
    } catch (error) {
      if (dom.contextChapterList) dom.contextChapterList.innerHTML = `<div class="context-inspector-empty">读取失败：${esc(error.message)}</div>`;
    }
  }

  function closeContextInspector() {
    if (dom.contextInspectorDialog?.close) dom.contextInspectorDialog.close();
    else dom.contextInspectorDialog?.removeAttribute('open');
  }

  function contextBudgetDescription() {
    const budget = state.lastContextBudget;
    if (!budget) return '发送一条消息后，这里会显示最近一次真正送入模型的本机估算。';
    const part = budget.breakdown || {};
    const history = budget.history || {};
    return [
      `最近一次预计输入 ${Number(budget.estimated_input_tokens || 0).toLocaleString()} tokens · ${budget.label || '已估算'}`,
      `可见对话 ${Number(part.messages || 0).toLocaleString()} 字 · 压缩旧历史 ${Number(part.compressed_history || 0).toLocaleString()} 字 · 关系与记忆 ${Number(part.dynamic_memory || 0).toLocaleString()} 字`,
      `近期原文 ${Number(history.selected_messages || 0).toLocaleString()} 条 / ${Number(history.selected_chars || 0).toLocaleString()} 字 · 硬上限 ${Number(history.max_chars || 0).toLocaleString()} 字`,
      `文件：本轮 ${Number(part.explicit_files || 0).toLocaleString()} 字 · 按需 ${Number(part.retrieved_files || 0).toLocaleString()} 字 · 常驻索引 ${Number(part.pinned_files || 0).toLocaleString()} 字`,
    ].join('\n');
  }

  const contextBlockLabels = {
    verified_tool_snapshot: '本机工具快照',
    artifact_persona: '人格底色卡',
    living_state: '此刻生活状态',
    inner_state_runtime: '统一内在状态',
    relationship_continuity: '关系与共同记忆',
    emotional_state: '情绪与亲密意图',
    conversation_chapters: '旧对话压缩章节',
    recalled_memory: '本轮相关记忆',
    sticker_catalog: '本地表情包目录',
    expression_integrity: '表达完整性提醒',
  };

  const contextModeLabels = {
    explicit: '本轮使用',
    retrieval: '按需摘取',
    pinned: '常驻索引',
  };

  function renderContextReceipt(budget = state.lastContextBudget) {
    const stack = budget?.dynamic_stack || {};
    const breakdown = budget?.breakdown || {};
    const history = budget?.history || {};
    const included = Array.isArray(stack.included) ? stack.included : [];
    const dropped = Array.isArray(stack.dropped) ? stack.dropped : [];
    if (dom.contextStackList) {
      const rows = [
        ...(budget ? [
          `<div class="context-stack-item is-core"><strong>基础人格与系统规则</strong><small>固定底层 · 始终带入</small><em>${Number(breakdown.system || 0).toLocaleString()} 字</em></div>`,
          `<div class="context-stack-item is-core"><strong>近期可见对话</strong><small>${Number(history.selected_messages || 0).toLocaleString()} 条 · 最多 ${Number(history.max_chars || 0).toLocaleString()} 字${Number(history.dropped_messages || 0) ? ` · 已停止向前 ${Number(history.dropped_messages).toLocaleString()} 条` : ''}${Number(history.truncated_messages || 0) ? ` · ${Number(history.truncated_messages).toLocaleString()} 条仅留首尾` : ''}</small><em>${Number(breakdown.messages || 0).toLocaleString()} 字</em></div>`,
        ] : []),
        ...included.map((item) => `
          <div class="context-stack-item${item.clipped ? ' is-clipped' : ''}">
            <strong>${esc(contextBlockLabels[item.name] || item.name || '动态内容')}</strong>
            <small>${item.clipped ? '已按预算截短' : '完整带入'} · 优先级 ${Number(item.priority || 0)}</small>
            <em>${Number(item.chars || 0).toLocaleString()} 字</em>
          </div>`),
        ...dropped.map((name) => `
          <div class="context-stack-item is-dropped">
            <strong>${esc(contextBlockLabels[name] || name || '动态内容')}</strong>
            <small>本轮没有进入 · 预算已满</small><em>省略</em>
          </div>`),
      ];
      const budgetNote = Number(stack.budget_chars || 0)
        ? `<div class="context-receipt-summary">动态层使用 ${Number(stack.used_chars || 0).toLocaleString()} / ${Number(stack.budget_chars || 0).toLocaleString()} 字</div>`
        : '';
      dom.contextStackList.innerHTML = rows.length
        ? budgetNote + rows.join('')
        : '<div class="context-inspector-empty">发送消息后显示关系、记忆、旧历史与状态层。</div>';
    }

    const files = Array.isArray(budget?.file_sources) ? budget.file_sources : [];
    if (dom.contextFileList) {
      dom.contextFileList.innerHTML = files.length ? files.map((item) => {
        const mode = ['explicit', 'retrieval', 'pinned'].includes(item.mode) ? item.mode : 'explicit';
        const chars = Number(item.available_chars || 0);
        const delivery = String(item.delivery_label || '').trim();
        const detail = delivery
          ? `${esc(item.kind || '文件')} · ${esc(delivery)}${chars ? ` · ${chars.toLocaleString()} 字可用` : ''}`
          : (item.native
            ? `${esc(item.kind || '文件')} · 原生内容已送入${chars ? ` · ${chars.toLocaleString()} 字可用` : ''}`
            : `${esc(item.kind || '文件')} · ${chars.toLocaleString()} 字可用`);
        return `<div class="context-file-item mode-${mode}"><strong title="${esc(item.name || '附件')}">${esc(item.name || '附件')}</strong><small>${detail}</small><em>${contextModeLabels[mode]}</em></div>`;
      }).join('') : '<div class="context-inspector-empty">这一轮没有带入文件。</div>';
    }
  }

  function renderContextActualUsage() {
    if (!dom.contextActualUsage) return;
    const usage = state.lastContextUsage;
    if (!usage) {
      dom.contextActualUsage.className = 'context-actual-usage';
      dom.contextActualUsage.textContent = '实际 Token 会在模型返回 usage 后补到这里。';
      return;
    }
    const input = Number(usage.input_tokens || 0);
    const output = Number(usage.output_tokens || 0);
    const cacheRead = Number(usage.cache_read || 0);
    const reasoning = Number(usage.reasoning_tokens || 0);
    const parts = [
      `API 实报：输入 ${input.toLocaleString()} tokens`,
      `输出 ${output.toLocaleString()}`,
    ];
    if (cacheRead) parts.push(`缓存命中 ${cacheRead.toLocaleString()}`);
    if (reasoning) parts.push(`推理 ${reasoning.toLocaleString()}`);
    if (Number.isFinite(Number(usage.cost))) parts.push(`费用 $${Number(usage.cost || 0).toFixed(4)}`);
    dom.contextActualUsage.className = 'context-actual-usage has-usage';
    dom.contextActualUsage.textContent = parts.join(' · ');
  }

  function renderContextInspector(data = {}) {
    if (!dom.contextInspectorDialog) return;
    renderContextReceipt(state.lastContextBudget);
    renderContextActualUsage();
    if (dom.contextInspectorBudget) dom.contextInspectorBudget.textContent = contextBudgetDescription();
    const unsaved = Boolean(data.unsaved);
    const actionsLocked = unsaved || data.enabled === false;
    if (dom.contextCompressionEnabled) {
      dom.contextCompressionEnabled.checked = data.enabled !== false;
      dom.contextCompressionEnabled.disabled = unsaved;
    }
    if (dom.btnContextCompact) dom.btnContextCompact.disabled = actionsLocked;
    if (dom.btnContextRebuild) dom.btnContextRebuild.disabled = actionsLocked;
    if (dom.contextCompressionStats) {
      const ratio = Number(data.compression_ratio || 1);
      dom.contextCompressionStats.innerHTML = `
        <div><span>原文安全上限</span><strong>${Number(data.raw_message_limit || 500).toLocaleString()} 条</strong><small>实际按 Token / 字符预算与稳定锚点选择</small></div>
        <div><span>增量章节</span><strong>${Number(data.chapters || 0).toLocaleString()} 个</strong><small>${Number(data.source_messages_compacted || 0).toLocaleString()} 条旧消息</small></div>
        <div><span>预计节省</span><strong>${Number(data.estimated_saved_tokens || 0).toLocaleString()} t</strong><small>${ratio > 1 ? `${ratio.toFixed(1)}× 压缩` : '尚未需要压缩'}</small></div>
        <div><span>待整理</span><strong>${Number(data.backlog_messages || 0).toLocaleString()} 条</strong><small>${esc(data.label || '等待状态')}</small></div>`;
    }
    const chapters = Array.isArray(data.recent_chapters) ? data.recent_chapters : [];
    if (dom.contextChapterList) {
      dom.contextChapterList.innerHTML = chapters.length ? chapters.map((chapter) => `
        <article class="context-chapter-row" data-chapter-id="${Number(chapter.id || 0)}">
          <div><h4>原消息 #${Number(chapter.start_message_id || 0)}–#${Number(chapter.end_message_id || 0)}</h4><p>${esc(chapter.summary || '')}</p><small>${Number(chapter.message_count || 0)} 条 · ${Number(chapter.source_chars || 0).toLocaleString()} → ${Number(chapter.summary_chars || 0).toLocaleString()} 字</small></div>
          <button class="btn-soft context-view-sources" type="button">核对原文</button>
        </article>`).join('') : `<div class="context-inspector-empty">${unsaved ? '发送第一条消息后，这个窗口会开始记录上下文状态。' : '近期原文窗口还够用，目前不需要生成压缩章节。'}</div>`;
      dom.contextChapterList.querySelectorAll('.context-view-sources').forEach((button) => {
        button.addEventListener('click', () => {
          const chapterId = button.closest('[data-chapter-id]')?.dataset.chapterId;
          if (chapterId) loadContextChapterSources(chapterId);
        });
      });
    }
  }

  async function updateContextCompressionEnabled() {
    if (!state.currentSession || !dom.contextCompressionEnabled) return;
    const enabled = dom.contextCompressionEnabled.checked;
    dom.contextCompressionEnabled.disabled = true;
    try {
      const response = await fetch(`/api/context-compression/${encodeURIComponent(state.currentSession)}`, {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || data.error || '保存失败');
      state.contextCompression = data;
      renderContextInspector(data);
    } catch (error) {
      dom.contextCompressionEnabled.checked = !enabled;
      alert(`上下文设置保存失败：${error.message}`);
    } finally {
      dom.contextCompressionEnabled.disabled = false;
    }
  }

  async function runContextCompaction() {
    if (!state.currentSession || !dom.btnContextCompact) return;
    dom.btnContextCompact.disabled = true;
    dom.btnContextCompact.textContent = '正在整理…';
    try {
      const response = await fetch(`/api/context-compression/${encodeURIComponent(state.currentSession)}/compact`, { method: 'POST' });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || data.error || '整理失败');
      await openContextInspector();
    } catch (error) {
      alert(`整理失败：${error.message}`);
    } finally {
      dom.btnContextCompact.disabled = false;
      dom.btnContextCompact.textContent = '立即补齐';
    }
  }

  async function rebuildContextCompaction() {
    if (!state.currentSession || !dom.btnContextRebuild) return;
    if (!confirm('重新生成这个窗口的派生章节吗？原消息、记忆和聊天窗口都不会被删除。')) return;
    dom.btnContextRebuild.disabled = true;
    dom.btnContextRebuild.textContent = '正在重建…';
    try {
      const response = await fetch(`/api/context-compression/${encodeURIComponent(state.currentSession)}/rebuild`, { method: 'POST' });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || data.error || '重建失败');
      state.contextCompression = data;
      renderContextInspector(data);
    } catch (error) {
      alert(`重建失败：${error.message}`);
    } finally {
      dom.btnContextRebuild.disabled = false;
      dom.btnContextRebuild.textContent = '重建派生章节';
    }
  }

  async function loadContextChapterSources(chapterId) {
    if (!state.currentSession || !dom.contextSourcePanel) return;
    dom.contextSourcePanel.classList.remove('hidden');
    dom.contextSourcePanel.innerHTML = '<div><h4>章节原文</h4></div><div class="context-inspector-empty">正在读取…</div>';
    try {
      const response = await fetch(`/api/context-compression/${encodeURIComponent(state.currentSession)}/chapters/${encodeURIComponent(chapterId)}/sources`, { cache: 'no-store' });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || data.error || '读取失败');
      const messages = Array.isArray(data.messages) ? data.messages : [];
      dom.contextSourcePanel.innerHTML = `<div><h4>章节原文 · ${messages.length} 条</h4><button class="btn-soft" type="button" id="context-source-close">收起</button></div><div class="context-source-messages">${messages.map((item) => `<article class="context-source-message"><strong>${item.role === 'user' ? '用户' : '助手'} · #${Number(item.id || 0)}</strong><p>${esc(item.content || '')}</p></article>`).join('')}</div>`;
      $('#context-source-close')?.addEventListener('click', () => dom.contextSourcePanel.classList.add('hidden'));
    } catch (error) {
      dom.contextSourcePanel.innerHTML = `<div><h4>章节原文</h4></div><div class="context-inspector-empty">读取失败：${esc(error.message)}</div>`;
    }
  }

  function ensureThinkingPanel(messageEl, options = {}) {
    if (!messageEl) return null;
    const host = messageEl.querySelector('.message-thinking-host');
    if (!host) return null;
    let details = host.querySelector('.thinking-panel');
    if (!details) {
      details = document.createElement('details');
      details.className = 'thinking-panel';
      details.innerHTML = `
        <summary><span class="thinking-pulse"><i></i></span><strong>${esc(options.label || '模型 API 可见推理')}</strong><em class="thinking-count">${options.chars ? `${Number(options.chars).toLocaleString()} 字` : '实时展开'}</em><b>⌄</b></summary>
        <div class="thinking-body"><p class="thinking-warning">这是供应商 API 明确返回的可见推理或摘要，不等于完整隐藏思考，也不保证正确；仅保存在本机，不会作为聊天内容交给其他模型。</p><pre></pre><span class="thinking-loading hidden">正在从本机读取…</span></div>`;
      host.appendChild(details);
      details.open = Boolean(options.open);
      if (options.provider) details.dataset.provider = String(options.provider);
      if (options.messageId) {
        details.dataset.messageId = String(options.messageId);
        details.dataset.loaded = 'false';
        details.addEventListener('toggle', async () => {
          if (!details.open || details.dataset.loaded === 'true' || details.dataset.loading === 'true') return;
          details.dataset.loading = 'true';
          const loading = details.querySelector('.thinking-loading');
          loading?.classList.remove('hidden');
          try {
            const data = await fetchJSON(`/api/messages/${encodeURIComponent(options.messageId)}/thinking`, { cache: 'no-store' });
            details.querySelector('pre').textContent = data.content || '';
            details.querySelector('.thinking-count').textContent = `${Number(data.char_count || 0).toLocaleString()} 字`;
            if (data.label) details.querySelector('summary strong').textContent = data.label;
            if (data.warning) details.querySelector('.thinking-warning').textContent = data.warning;
            if (data.provider) details.dataset.provider = String(data.provider);
            details.dataset.loaded = 'true';
          } catch (error) {
            details.querySelector('pre').textContent = `读取失败：${error.message}`;
          } finally {
            loading?.classList.add('hidden');
            details.dataset.loading = 'false';
          }
        });
      } else {
        details.dataset.loaded = 'true';
      }
    }
    if (options.label) details.querySelector('summary strong').textContent = options.label;
    if (options.provider) details.dataset.provider = String(options.provider);
    return details;
  }

  function appendThinkingDelta(messageEl, text, options = {}) {
    if (!text) return;
    const details = ensureThinkingPanel(messageEl, { ...options, open: true });
    if (!details) return;
    details.open = true;
    const pre = details.querySelector('pre');
    pre.textContent += text;
    details.querySelector('.thinking-count').textContent = `${pre.textContent.length.toLocaleString()} 字`;
  }

  function setMessageText(messageEl, text) {
    if (!messageEl) return;
    const bubble = messageEl.querySelector('.bubble');
    if (!bubble) return;
    const source = String(text || '');
    messageEl._rawText = source;
    bubble.innerHTML = renderMarkdown(source);
    decorateRenderedMarkdown(bubble);
    bubble.classList.toggle('hidden', !text);
  }

  function appendAttachmentToMessage(messageEl, item) {
    if (!messageEl || !item) return;
    const media = messageEl.querySelector('.message-media');
    if (!media) return;
    if (item.kind === 'image') {
      const link = document.createElement('a');
      link.className = 'message-image-link';
      link.href = item.preview_url || '#';
      link.target = '_blank';
      link.rel = 'noopener';
      const img = document.createElement('img');
      img.className = 'message-image';
      img.src = item.preview_url || '';
      img.alt = item.name || '图片';
      link.appendChild(img);
      media.appendChild(link);
      return;
    }
    if (item.kind === 'audio') {
      const card = document.createElement('div');
      card.className = 'message-audio-card';
      const status = audioAnalysisLabel(item);
      card.innerHTML = `
        <div class="message-audio-head">
          <span>WAVE</span>
          <div class="message-audio-copy"><strong>${esc(item.name || '音频')}</strong><small>${esc(formatBytes(item.size || 0))} · ${esc(status)}</small></div>
        </div>
        <audio controls preload="metadata" src="${esc(item.preview_url || '')}"></audio>
        <div class="message-audio-links">
          ${item.report_url ? `<a href="${esc(item.report_url)}" target="_blank" rel="noopener">完整听海报告</a>` : ''}
          ${item.spectrogram_url ? `<a href="${esc(item.spectrogram_url)}" target="_blank" rel="noopener">频谱图</a>` : ''}
        </div>`;
      media.appendChild(card);
      return;
    }
    const link = document.createElement('a');
    link.className = 'message-file-card';
    link.href = item.preview_url || '#';
    link.target = '_blank';
    link.rel = 'noopener';
    const icon = item.kind === 'pdf' ? 'PDF' : (item.kind === 'text' ? 'TXT' : 'FILE');
    link.innerHTML = `
      <span class="file-kind">${esc(icon)}</span>
      <span class="file-main"><strong>${esc(item.name || '附件')}</strong><small>${esc(formatBytes(item.size || 0))} · ${esc(item.parse_message || item.status || '')}</small></span>
    `;
    media.appendChild(link);
  }

  function appendStickerToMessage(messageEl, sticker) {
    if (!messageEl || !sticker) return;
    const media = messageEl.querySelector('.message-media');
    if (!media || media.querySelector(`[data-sticker-id="${cssEscape(sticker.id || '')}"]`)) return;
    const img = document.createElement('img');
    img.className = 'message-sticker';
    img.dataset.stickerId = sticker.id || '';
    img.src = sticker.url || '';
    img.alt = sticker.name || '表情包';
    img.title = sticker.name || '表情包';
    media.appendChild(img);
  }

  function appendTyping() {
    removeEmptyState();
    const el = document.createElement('div');
    el.className = 'message assistant';
    el.dataset.companionInitial = companionInitial;
    el.innerHTML = `<div class="typing-indicator"><span></span><span></span><span></span></div>`;
    dom.messages.appendChild(el);
    scrollToBottom();
    return el;
  }

  function scrollToBottom({
    instant = false,
    expectedSession = null,
    expectedHistoryEpoch = null,
  } = {}) {
    requestAnimationFrame(() => {
      if (expectedSession && expectedSession !== state.currentSession) return;
      if (
        Number.isInteger(expectedHistoryEpoch)
        && expectedHistoryEpoch !== state.historyNavigationEpoch
      ) return;
      const previousScrollBehavior = dom.messages.style.scrollBehavior;
      if (instant) dom.messages.style.scrollBehavior = 'auto';
      dom.messages.scrollTop = dom.messages.scrollHeight;
      if (instant) {
        requestAnimationFrame(() => {
          dom.messages.style.scrollBehavior = previousScrollBehavior;
        });
      }
    });
  }

  function updateChatHistoryProgress() {
    if (!dom.messages || !dom.chatHistoryNav) return;
    const max = Math.max(0, dom.messages.scrollHeight - dom.messages.clientHeight);
    const total = Math.max(0, Number(state.totalMessageCount || 0));
    const start = Math.max(0, Number(state.historyStartPosition || 0));
    const end = Math.max(start, Number(state.historyEndPosition || 0));
    let absolutePosition = 0;
    let ratio = 0;
    if (total > 0 && max - dom.messages.scrollTop <= 4 && end >= total) {
      absolutePosition = total;
    } else if (total > 0) {
      const topLine = dom.messages.getBoundingClientRect().top + 4;
      const visible = [...dom.messages.querySelectorAll('.message[data-history-position]')]
        .find((element) => element.getBoundingClientRect().bottom > topLine);
      absolutePosition = Number(visible?.dataset.historyPosition || start || 1);
    }
    if (total > 1) {
      ratio = Math.min(1, Math.max(0, (absolutePosition - 1) / (total - 1)));
    } else if (total === 1) {
      absolutePosition = 1;
      ratio = 1;
    }
    let percent = Math.round(ratio * 100);
    if (total > 1) {
      if (absolutePosition <= 1) percent = 0;
      else if (absolutePosition >= total) percent = 100;
      else percent = Math.min(99, Math.max(1, percent));
    }
    dom.chatHistoryFill?.style.setProperty('height', `${percent}%`);
    dom.chatHistoryThumb?.style.setProperty('top', `${percent}%`);
    if (dom.chatHistoryPercent) dom.chatHistoryPercent.textContent = `${percent}%`;
    const current = total > 0 ? Math.max(1, Math.min(total, Math.round(absolutePosition || total))) : 0;
    if (dom.chatHistoryTrack) {
      const label = total > 0
        ? `聊天阅读进度：第 ${current.toLocaleString()} / ${total.toLocaleString()} 条`
        : '聊天阅读进度：空对话';
      dom.chatHistoryTrack.title = label;
      dom.chatHistoryTrack.setAttribute('aria-label', label);
    }
    dom.chatHistoryNav.classList.toggle(
      'is-at-start',
      total > 0 && start <= 1 && dom.messages.scrollTop <= 4,
    );
    dom.chatHistoryNav.classList.toggle(
      'is-at-end',
      total === 0 || (end >= total && max - dom.messages.scrollTop <= 4),
    );
  }

  async function loadHistoryWindowAtPosition(targetPosition, { oldest = false } = {}) {
    if (!state.currentSession) return { ok: false, reason: 'no-session' };
    if (state.isStreaming) return { ok: false, reason: 'streaming' };
    if (state.loadingOlderMessages) return { ok: false, reason: 'busy' };
    const activeSession = state.currentSession;
    const activeEpoch = state.historyNavigationEpoch;
    const controller = new AbortController();
    const renderToken = ++state.historyRenderToken;
    state.historyPageController = controller;
    state.loadingOlderMessages = true;
    const requested = Math.max(1, Number(targetPosition || 1));
    const query = oldest
      ? 'oldest=true'
      : `position=${encodeURIComponent(Math.trunc(requested))}`;
    try {
      const response = await fetch(
        `/api/sessions/${encodeURIComponent(activeSession)}/message-page?limit=${state.messagePageSize}&${query}`,
        { cache: 'no-store', signal: controller.signal },
      );
      const page = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(page.detail || '聊天记录读取失败');
      if (
        activeSession !== state.currentSession
        || activeEpoch !== state.historyNavigationEpoch
        || state.historyPageController !== controller
      ) {
        return { ok: false, reason: 'session-changed' };
      }
      if (Number(page.total_count || 0) > 0 && !Array.isArray(page.items)) {
        throw new Error('聊天记录分页格式无效');
      }
      renderHistoryMessagePage(page);
      const actualTarget = oldest
        ? 1
        : Math.max(1, Number(page.requested_position || requested));
      const previous = dom.messages.style.scrollBehavior;
      dom.messages.style.scrollBehavior = 'auto';
      const target = dom.messages.querySelector(`[data-history-position="${actualTarget}"]`);
      if (oldest || actualTarget <= 1) {
        dom.messages.scrollTop = 0;
      } else if (target) {
        dom.messages.scrollTop = Math.max(
          0,
          target.offsetTop - ((dom.messages.clientHeight - target.offsetHeight) / 2),
        );
      } else {
        const span = Math.max(1, state.historyEndPosition - state.historyStartPosition);
        const localRatio = Math.min(
          1,
          Math.max(0, (actualTarget - state.historyStartPosition) / span),
        );
        dom.messages.scrollTop = Math.max(
          0,
          (dom.messages.scrollHeight - dom.messages.clientHeight) * localRatio,
        );
      }
      requestAnimationFrame(() => {
        if (
          activeSession !== state.currentSession
          || activeEpoch !== state.historyNavigationEpoch
          || renderToken !== state.historyRenderToken
        ) return;
        dom.messages.style.scrollBehavior = previous;
        updateChatHistoryProgress();
      });
      return { ok: true, page };
    } catch (error) {
      if (
        activeSession !== state.currentSession
        || activeEpoch !== state.historyNavigationEpoch
        || error?.name === 'AbortError'
      ) {
        return { ok: false, reason: 'session-changed' };
      }
      return { ok: false, reason: 'error', error };
    } finally {
      if (state.historyPageController === controller) {
        state.historyPageController = null;
        state.loadingOlderMessages = false;
      }
    }
  }

  async function jumpToConversationStart() {
    const session = state.sessions.find((item) => item.id === state.currentSession);
    if (
      !state.currentSession
      || state.isStreaming
      || state.loadingOlderMessages
      || Number(state.totalMessageCount || 0) <= 0
      || session?.persisted === false
    ) {
      updateChatHistoryProgress();
      return;
    }
    const activeSession = state.currentSession;
    const activeEpoch = state.historyNavigationEpoch;
    const button = dom.chatJumpStart;
    if (button) { button.disabled = true; button.textContent = '…'; }
    try {
      const result = await loadHistoryWindowAtPosition(1, { oldest: true });
      if (
        !result.ok
        && result.reason === 'error'
        && activeSession === state.currentSession
        && activeEpoch === state.historyNavigationEpoch
      ) {
        dom.headerStatus.textContent = `回到最前面失败：${result.error?.message || '读取失败'}`;
      }
    } finally {
      if (
        button
        && activeSession === state.currentSession
        && activeEpoch === state.historyNavigationEpoch
      ) {
        button.disabled = false;
        button.textContent = '↑';
      }
    }
  }

  async function jumpToConversationEnd() {
    if (!state.currentSession || state.loadingOlderMessages) return;
    const activeSession = state.currentSession;
    const activeEpoch = state.historyNavigationEpoch;
    const button = dom.chatJumpEnd;
    if (button) { button.disabled = true; button.textContent = '…'; }
    try {
      let result = { ok: true };
      if (!state.historyWindowAtLatest) {
        result = await loadHistoryWindowAtPosition(
          Math.max(1, Number(state.totalMessageCount || 1)),
        );
      }
      if (
        !result.ok
        && result.reason === 'error'
        && activeSession === state.currentSession
        && activeEpoch === state.historyNavigationEpoch
      ) {
        dom.headerStatus.textContent = `回到最后面失败：${result.error?.message || '读取失败'}`;
        return;
      }
      if (activeSession !== state.currentSession || activeEpoch !== state.historyNavigationEpoch) return;
      scrollToBottom({
        instant: true,
        expectedSession: activeSession,
        expectedHistoryEpoch: activeEpoch,
      });
      requestAnimationFrame(updateChatHistoryProgress);
    } finally {
      if (
        button
        && activeSession === state.currentSession
        && activeEpoch === state.historyNavigationEpoch
      ) {
        button.disabled = false;
        button.textContent = '↓';
      }
    }
  }

  async function jumpWithinConversationHistory(event) {
    if (!dom.messages || !dom.chatHistoryTrack) return;
    const rect = dom.chatHistoryTrack.getBoundingClientRect();
    const ratio = Math.min(1, Math.max(0, (event.clientY - rect.top) / Math.max(1, rect.height)));
    const total = Math.max(0, Number(state.totalMessageCount || 0));
    if (total <= 1) return;
    const targetPosition = Math.round(ratio * (total - 1)) + 1;
    const start = Number(state.historyStartPosition || 0);
    const end = Number(state.historyEndPosition || 0);
    if (start > 0 && targetPosition >= start && targetPosition <= end) {
      const candidates = [...dom.messages.querySelectorAll('.message[data-history-position]')];
      const target = candidates.reduce((best, element) => {
        const distance = Math.abs(Number(element.dataset.historyPosition || 0) - targetPosition);
        return !best || distance < best.distance ? { element, distance } : best;
      }, null)?.element;
      if (target) {
        dom.messages.scrollTo({ top: Math.max(0, target.offsetTop - 8), behavior: 'smooth' });
      }
      return;
    }
    const activeSession = state.currentSession;
    const activeEpoch = state.historyNavigationEpoch;
    dom.chatHistoryTrack.disabled = true;
    try {
      const result = await loadHistoryWindowAtPosition(targetPosition);
      if (
        !result.ok
        && result.reason === 'error'
        && activeSession === state.currentSession
        && activeEpoch === state.historyNavigationEpoch
      ) {
        dom.headerStatus.textContent = `定位聊天进度失败：${result.error?.message || '读取失败'}`;
      }
    } finally {
      if (
        activeSession === state.currentSession
        && activeEpoch === state.historyNavigationEpoch
      ) dom.chatHistoryTrack.disabled = false;
    }
  }


  function renderEmptyState() {
    dom.messages.innerHTML = `
      <div class="empty-state">
        <div class="reader-empty-love" aria-label="Welcome back, my love.">
          <span class="reader-empty-eyebrow">A PLACE THAT IS ALWAYS YOURS</span>
          <div class="reader-empty-script">Welcome back, my love.</div>
          <p>I kept a quiet place for every word your heart still wants to say.</p>
        </div>
        <div class="empty-orbit" aria-hidden="true">
          <span class="empty-state-icon"><i></i></span>
          <b></b><b></b><b></b>
        </div>
        <div class="empty-state-kicker">YOUR PRIVATE SPACE</div>
        <div class="empty-state-title">今晚也给你留着一盏灯</div>
        <div class="empty-state-text">难受的、开心的、想不通的，都可以慢慢放进来。</div>
        <div class="empty-state-chips">
          <button class="empty-state-chip" type="button" data-prompt="陪我随便聊聊吧">随便说说</button>
          <button class="empty-state-chip" type="button" data-prompt="帮我理清一件事">整理思绪</button>
          <button class="empty-state-chip" type="button" data-prompt="我们继续上次的话题">继续上次</button>
        </div>
        <div class="empty-state-foot"><span></span>本地优先 · 私人记忆</div>
      </div>
    `;
    dom.messages.querySelectorAll('.empty-state-chip').forEach((button) => {
      button.addEventListener('click', () => {
        dom.input.value = button.dataset.prompt || '';
        autoResize(dom.input);
        updateSendState();
        dom.input.focus();
      });
    });
  }

  function removeEmptyState() {
    const empty = dom.messages.querySelector('.empty-state');
    if (empty) empty.remove();
  }

  // ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  //  v5.7 附件与表情包
  // ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  function updateSendState() {
    if (!dom.btnSend) return;
    const canStopServerRequest = Boolean(
      state.isStreaming || (
        state.recoveringPendingRequest
        && window.ChatReliability?.pending?.()?.client_request_id
      )
    );
    dom.btnSend.classList.toggle('is-stop', canStopServerRequest);
    dom.btnSend.setAttribute('aria-label', canStopServerRequest ? '停止生成' : '发送消息');
    dom.btnSend.title = canStopServerRequest ? '停止生成' : '发送消息';
    if (state.isStreaming) {
      dom.btnSend.disabled = !state.activeChatController || state.stopRequested;
      return;
    }
    if (state.recoveringPendingRequest) {
      // Reconnected clients can still explicitly stop the detached producer.
      // Merely disconnecting never triggers this path by itself.
      dom.btnSend.disabled = !canStopServerRequest || state.stopRequested;
      return;
    }
    const hasPayload = Boolean(dom.input.value.trim() || state.pendingAttachments.length);
    const blockedAudio = state.pendingAttachments.find((item) => (
      item.kind === 'audio' && item.analysis_status !== 'ready'
    ));
    const blockedVideo = state.pendingAttachments.find((item) => (
      item.kind === 'video' && Number(item.video_frame_count || 0) <= 0
    ));
    dom.btnSend.disabled = !hasPayload || state.uploadingAttachments > 0 || Boolean(blockedAudio) || Boolean(blockedVideo);
    if (blockedAudio) {
      const failed = blockedAudio.analysis_status === 'error';
      dom.btnSend.setAttribute('aria-label', failed ? '音频分析失败，请重试' : '等待听海听完音频');
      dom.btnSend.title = failed ? '这段音频没有听完，请先重新分析' : '听海显示“已听完”后才能发送';
    } else if (blockedVideo) {
      dom.btnSend.setAttribute('aria-label', '视频读取失败');
      dom.btnSend.title = blockedVideo.parse_message || '视频没有成功抽取关键帧，请确认本机 ffmpeg 可用后重新上传';
    }
  }

  async function handleAttachmentSelection(event) {
    const files = Array.from(event.target.files || []).slice(0, 8);
    event.target.value = '';
    for (const file of files) {
      state.uploadingAttachments += 1;
      updateAttachmentHint();
      updateSendState();
      try {
        const form = new FormData();
        form.append('file', file);
        const resp = await fetch('/api/attachments/upload', { method: 'POST', body: form });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || data.detail || '上传失败');
        state.pendingAttachments.push(data);
        renderAttachmentTray();
        await loadFileWorkspace();
        if (data.kind === 'audio') startOceanPolling();
      } catch (e) {
        alert(`附件 ${file.name} 上传失败：${e.message}`);
      } finally {
        state.uploadingAttachments -= 1;
        updateAttachmentHint();
        updateSendState();
      }
    }
  }

  function updateAttachmentHint() {
    if (!dom.attachmentHint) return;
    dom.attachmentHint.textContent = state.uploadingAttachments > 0
      ? `正在解析 ${state.uploadingAttachments} 个附件…`
      : '支持 ZIP 本机解析、视频关键帧、唱歌音频、原图、PDF、Office、EPUB、文字与代码；消息中的网页链接会自动读取正文。';
  }

  function renderAttachmentTray() {
    if (!dom.attachmentTray) return;
    dom.attachmentTray.classList.toggle('hidden', state.pendingAttachments.length === 0);
    dom.attachmentTray.innerHTML = state.pendingAttachments.map((item) => {
      if (item.kind === 'audio') {
        const status = String(item.analysis_status || 'waiting_install');
        const progress = Math.max(0, Math.min(100, Number(item.analysis_progress || 0)));
        const actions = status === 'waiting_install'
          ? '<button class="pending-audio-install" type="button">安装本机听觉</button>'
          : (status === 'error' ? '<button class="pending-audio-retry" type="button">重新分析</button>' : '');
        return `
          <div class="pending-attachment audio is-${esc(status)}" data-id="${esc(item.id)}">
            <span class="pending-file-kind">WAVE</span>
            <span class="pending-file-main"><strong>${esc(item.name || '音频')}</strong><small>${esc(audioAnalysisLabel(item))}</small></span>
            <button class="pending-remove" type="button" title="移出本轮">×</button>
            <audio class="pending-audio-player" controls preload="metadata" src="${esc(item.preview_url || '')}"></audio>
            <span class="pending-audio-progress" style="--ocean-progress:${progress}%"><i></i></span>
            ${actions ? `<span class="pending-audio-actions">${actions}</span>` : ''}
          </div>`;
      }
      return `
        <div class="pending-attachment" data-id="${esc(item.id)}">
          ${item.kind === 'image' ? `<img src="${esc(item.preview_url || '')}" alt="">` : `<span class="pending-file-kind">${esc(item.kind === 'pdf' ? 'PDF' : (item.kind === 'archive' ? 'ZIP' : 'FILE'))}</span>`}
          <span class="pending-file-main"><strong>${esc(item.name || '附件')}</strong><small>${esc(item.kind === 'image' ? currentImageDeliveryLabel() : (item.parse_message || item.status || ''))}</small></span>
          <button class="pending-remove" type="button" title="移除">×</button>
        </div>`;
    }).join('');
    dom.attachmentTray.querySelectorAll('.pending-remove').forEach((button) => {
      button.addEventListener('click', () => {
        const row = button.closest('.pending-attachment');
        const id = row?.dataset.id;
        state.pendingAttachments = state.pendingAttachments.filter((item) => item.id !== id);
        renderAttachmentTray();
        updateSendState();
        // 这里只移出下一条消息；文件仍留在资料工作室，避免误删历史资料。
      });
    });
    dom.attachmentTray.querySelectorAll('.pending-audio-install').forEach((button) => {
      button.addEventListener('click', installOceanListen);
    });
    dom.attachmentTray.querySelectorAll('.pending-audio-retry').forEach((button) => {
      button.addEventListener('click', () => retryAudioAnalysis(button.closest('.pending-attachment')?.dataset.id));
    });
  }

  function audioAnalysisLabel(item) {
    const status = String(item?.analysis_status || 'waiting_install');
    const stage = String(item?.analysis_stage || '').trim();
    const progress = Math.max(0, Math.min(100, Number(item?.analysis_progress || 0)));
    if (status === 'ready') return '听海已听完 · 报告可送给所有模型';
    if (status === 'error') return `没有听完 · ${item?.analysis_error || item?.parse_message || '请重新分析'}`;
    if (status === 'running') return `${stage || '本机分析中'} · ${progress}%`;
    if (status === 'queued') return '已进入本机队列 · 等待轮到它';
    return '等待安装一次“听海 · 本机听觉”';
  }

  function currentImageDeliveryLabel() {
    const protocol = String(state.providers?.[state.activeProvider]?.protocol || '');
    return ['openai_responses', 'anthropic'].includes(protocol)
      ? '当前通道会把原图送入模型'
      : '当前通道不会发送原图 · 请切换 Claude/GPT 视觉通道';
  }

  function workspaceKindLabel(item) {
    const kind = String(item?.kind || 'file');
    if (kind === 'image') return 'IMG';
    if (kind === 'audio') return 'WAVE';
    if (kind === 'pdf') return 'PDF';
    if (kind === 'document') return /\.pptx$/i.test(item?.name || '') ? 'PPTX' : 'DOCX';
    if (kind === 'spreadsheet') return 'XLSX';
    if (kind === 'ebook') return 'EPUB';
    if (kind === 'archive') return 'ZIP';
    if (kind === 'text') return 'TEXT';
    return 'FILE';
  }

  function workspaceRowHTML(item) {
    const kind = String(item.kind || 'file');
    const mode = ['retrieval', 'pinned'].includes(item.mode) ? item.mode : 'off';
    const audioReady = kind !== 'audio' || item.analysis_status === 'ready';
    const icon = kind === 'image'
      ? `<span class="workspace-kind image"><img src="${esc(item.preview_url || '')}" alt=""></span>`
      : `<span class="workspace-kind ${esc(kind)}">${esc(workspaceKindLabel(item))}</span>`;
    const tokenEstimate = Math.max(0, Math.ceil(Number(item.extracted_chars || 0) / 2.4));
    const modeNote = !audioReady
      ? `<em>${esc(audioAnalysisLabel(item))}</em>`
      : (mode === 'pinned'
      ? `<em class="danger">每轮最多重复约 ${tokenEstimate.toLocaleString()} tokens</em>`
      : (mode === 'retrieval'
        ? '<em>只在问题相关时发送少量片段</em>'
        : '<em>仅保存在资料库，不进入模型</em>'));
    const audioPlayer = kind === 'audio'
      ? `<audio class="workspace-file-audio" controls preload="metadata" src="${esc(item.preview_url || '')}"></audio>`
      : '';
    const audioAction = kind !== 'audio' ? '' : (
      item.analysis_status === 'waiting_install'
        ? '<button class="workspace-audio-install" type="button">安装听海</button>'
        : (item.analysis_status === 'error'
          ? '<button class="workspace-audio-retry" type="button">重新分析</button>'
          : (item.report_url ? `<a href="${esc(item.report_url)}" target="_blank" rel="noopener">完整报告</a>` : ''))
    );
    return `
      <article class="workspace-file-row kind-${esc(kind)} mode-${esc(mode)}" data-id="${esc(item.id)}">
        ${icon}
        <div class="workspace-file-copy">
          <strong title="${esc(item.name || '资料')}">${esc(item.name || '资料')}</strong>
          <small>${esc(formatBytes(item.size || 0))} · ${esc(item.parse_message || item.status || '已保存')}</small>
          ${modeNote}
          ${audioPlayer}
        </div>
        <div class="workspace-file-actions">
          <button class="workspace-use" type="button" ${audioReady ? '' : 'disabled'}>本轮使用</button>
          <label class="workspace-mode-label"><span>后续</span><select class="workspace-mode" aria-label="后续资料模式" ${audioReady ? '' : 'disabled'}>
            <option value="off" ${mode === 'off' ? 'selected' : ''}>仅资料库</option>
            <option value="retrieval" ${mode === 'retrieval' ? 'selected' : ''}>按需摘取</option>
            <option value="pinned" ${mode === 'pinned' ? 'selected' : ''}>常驻索引</option>
          </select></label>
          ${audioAction}
          <a href="${esc(item.preview_url || '#')}" target="_blank" rel="noopener">打开</a>
          <button class="workspace-delete danger-action" type="button" title="从本机彻底删除">彻底删除</button>
        </div>
      </article>`;
  }

  function bindWorkspaceList(container) {
    if (!container) return;
    container.querySelectorAll('.workspace-file-row').forEach((row) => {
      const id = row.dataset.id;
      const item = state.workspaceFiles.find((value) => value.id === id);
      row.querySelector('.workspace-use')?.addEventListener('click', () => {
        if (!item || state.pendingAttachments.some((value) => value.id === id)) return;
        if (item.kind === 'audio' && item.analysis_status !== 'ready') {
          alert('要等听海显示“已听完”后，才能把这段声音放进消息。');
          return;
        }
        state.pendingAttachments.push(item);
        renderAttachmentTray();
        updateSendState();
      });
      row.querySelector('.workspace-audio-install')?.addEventListener('click', installOceanListen);
      row.querySelector('.workspace-audio-retry')?.addEventListener('click', () => retryAudioAnalysis(id));
      row.querySelector('.workspace-mode')?.addEventListener('change', async (event) => {
        if (!item || !state.currentSession) return;
        const mode = event.target.value || 'off';
        if (mode === 'pinned' && !confirm(`“${item.name || '这份资料'}”会作为常驻索引保留，每轮只发送短清单和相关片段；完整正文仍留在本机。仍要设为常驻索引吗？`)) {
          event.target.value = item.mode || 'off';
          return;
        }
        const resp = await fetch(`/api/file-workspace/${encodeURIComponent(id)}/mode`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ session_id: state.currentSession, mode }),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) return alert(data.error || data.detail || '资料模式设置失败');
        await loadFileWorkspace();
      });
      row.querySelector('.workspace-delete')?.addEventListener('click', async () => {
        if (!item || !confirm(`从本机资料工作室删除“${item.name || '这份资料'}”吗？历史消息里的文件卡也会失去原文件。`)) return;
        const resp = await fetch(`/api/attachments/${encodeURIComponent(id)}`, { method: 'DELETE' });
        if (!resp.ok) {
          const data = await resp.json().catch(() => ({}));
          return alert(data.detail || '删除失败');
        }
        state.pendingAttachments = state.pendingAttachments.filter((value) => value.id !== id);
        renderAttachmentTray();
        await loadFileWorkspace();
      });
    });
  }

  function renderFileWorkspace() {
    const items = state.workspaceFiles || [];
    const content = items.length
      ? items.map(workspaceRowHTML).join('')
      : '<div class="workspace-empty">这里还没有资料。上传唱歌音频、照片、PDF、Office、EPUB、文字或代码后，它们会安静地留在本机。</div>';
    [dom.workspaceFileList, dom.workspaceDialogList].forEach((container) => {
      if (!container) return;
      container.innerHTML = content;
      bindWorkspaceList(container);
    });

    const stats = state.workspaceStats || {};
    const modes = state.workspaceModes || {};
    if (dom.workspaceFileCount) dom.workspaceFileCount.textContent = `${Number(stats.files || 0)} 份`;
    if (dom.workspaceRetrievalCount) dom.workspaceRetrievalCount.textContent = `${Number(modes.retrieval || 0)} 份`;
    if (dom.workspaceActiveCount) dom.workspaceActiveCount.textContent = `${Number(modes.pinned || 0)} 份`;
    if (dom.workspaceTotalSize) dom.workspaceTotalSize.textContent = formatBytes(stats.bytes || 0);
    const pinned = items.filter((item) => item.mode === 'pinned');
    const retrieval = items.filter((item) => item.mode === 'retrieval');
    const modeCount = Number(modes.pinned || 0) + Number(modes.retrieval || 0);
    dom.workspaceContextStrip?.classList.toggle('hidden', modeCount === 0);
    if (dom.workspaceContextLabel) {
      const labels = [];
      if (pinned.length || Number(modes.pinned || 0)) labels.push(`常驻索引 ${Number(modes.pinned || pinned.length)} 份`);
      if (retrieval.length || Number(modes.retrieval || 0)) labels.push(`按需摘取 ${Number(modes.retrieval || retrieval.length)} 份`);
      dom.workspaceContextLabel.textContent = labels.length ? labels.join(' · ') : '当前资料均只留在资料库';
    }
  }

  async function loadFileWorkspace(query = '') {
    const params = new URLSearchParams();
    if (state.currentSession) params.set('session_id', state.currentSession);
    if (query) params.set('query', query);
    const resp = await fetch(`/api/file-workspace?${params.toString()}`, { cache: 'no-store' });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || data.detail || '资料工作室读取失败');
    state.workspaceFiles = Array.isArray(data.items) ? data.items : [];
    state.workspaceStats = data.stats || { files: 0, bytes: 0, active: 0 };
    state.workspaceModes = data.modes || { off: 0, retrieval: 0, pinned: 0 };
    renderFileWorkspace();
    return data;
  }

  function renderOceanListenStatus(data = state.ocean) {
    if (!data) return;
    if (dom.oceanReadyBadge) {
      let label = '等待安装';
      let className = 'missing';
      if (!data.supported) { label = '需在 Mac 安装'; className = 'unsupported'; }
      if (data.installing) { label = '正在安装'; className = 'installing'; }
      if (data.installed) { label = '本机听觉就绪'; className = 'ready'; }
      dom.oceanReadyBadge.textContent = label;
      dom.oceanReadyBadge.className = `ocean-ready-badge ${className}`;
    }
    if (dom.oceanPrivacy) dom.oceanPrivacy.textContent = data.privacy || '原音频留在 Mac';
    if (dom.oceanQueue) dom.oceanQueue.textContent = `${Number(data.queued || 0)} 段`;
    if (dom.oceanNote) {
      dom.oceanNote.textContent = data.detail || '';
      dom.oceanNote.className = `ocean-note ${data.installed ? 'ok' : (data.installing ? '' : 'warn')}`;
    }
    if (dom.oceanInstallLog) {
      dom.oceanInstallLog.textContent = data.install_log_tail || '还没有安装记录。';
    }
    if (dom.btnOceanInstall) {
      dom.btnOceanInstall.hidden = Boolean(data.installed);
      dom.btnOceanInstall.disabled = Boolean(data.installing || !data.supported);
      dom.btnOceanInstall.textContent = data.installing ? '正在安装…' : '安装本机听觉';
    }
  }

  async function loadOceanListenStatus() {
    try {
      const data = await fetchJSON('/api/ocean-listen/status', { cache: 'no-store' });
      state.ocean = data;
      renderOceanListenStatus(data);
      if (data.installing || Number(data.queued || 0) > 0) startOceanPolling();
      return data;
    } catch (error) {
      if (dom.oceanReadyBadge) {
        dom.oceanReadyBadge.textContent = '状态读取失败';
        dom.oceanReadyBadge.className = 'ocean-ready-badge missing';
      }
      if (dom.oceanNote) {
        dom.oceanNote.textContent = `听海状态没有接上：${error.message}`;
        dom.oceanNote.className = 'ocean-note warn';
      }
      return null;
    }
  }

  async function installOceanListen() {
    const accepted = confirm(
      '第一次安装“听海 · 本机听觉”会在这台 Mac 单独下载 Python 3.10、声音分析组件和模型，可能需要数 GB 空间与较长时间。\n\n原音频和本机 Whisper 结果不会上传给模型供应商。现在开始吗？'
    );
    if (!accepted) return;
    if (dom.btnOceanInstall) dom.btnOceanInstall.disabled = true;
    try {
      const response = await fetch('/api/ocean-listen/install', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ confirm: true }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || data.detail || '听海安装没有启动');
      state.ocean = data;
      renderOceanListenStatus(data);
      if (dom.oceanLogDetails) dom.oceanLogDetails.open = true;
      startOceanPolling();
    } catch (error) {
      alert(`听海安装没有启动：${error.message}`);
      await loadOceanListenStatus();
    }
  }

  async function retryAudioAnalysis(attachmentId) {
    if (!attachmentId) return;
    try {
      const response = await fetch(`/api/audio-analysis/${encodeURIComponent(attachmentId)}/retry`, {
        method: 'POST',
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || data.detail || '重新分析没有启动');
      state.pendingAttachments = state.pendingAttachments.map((item) => item.id === attachmentId ? { ...item, ...data } : item);
      state.workspaceFiles = state.workspaceFiles.map((item) => item.id === attachmentId ? { ...item, ...data } : item);
      renderAttachmentTray();
      renderFileWorkspace();
      updateSendState();
      if (data.analysis_status === 'waiting_install') await installOceanListen();
      else startOceanPolling();
    } catch (error) {
      alert(`这段音频无法重新分析：${error.message}`);
    }
  }

  function startOceanPolling() {
    if (state.oceanPollTimer) return;
    refreshPendingAudioAnalyses();
    state.oceanPollTimer = setInterval(refreshPendingAudioAnalyses, 2200);
  }

  function stopOceanPolling() {
    if (!state.oceanPollTimer) return;
    clearInterval(state.oceanPollTimer);
    state.oceanPollTimer = null;
  }

  async function refreshPendingAudioAnalyses() {
    if (state.oceanRefreshing) return;
    state.oceanRefreshing = true;
    try {
      const combined = [...state.pendingAttachments, ...state.workspaceFiles];
      const ids = [...new Set(combined
        .filter((item) => item?.kind === 'audio' && item.analysis_status !== 'ready')
        .map((item) => item.id)
        .filter(Boolean))];
      const results = await Promise.all(ids.map(async (id) => {
        const response = await fetch(`/api/audio-analysis/${encodeURIComponent(id)}`, { cache: 'no-store' });
        if (!response.ok) return null;
        return response.json();
      }));
      const byId = new Map(results.filter(Boolean).map((item) => [item.id, item]));
      if (byId.size) {
        const previousStatus = new Map(
          [...state.pendingAttachments, ...state.workspaceFiles]
            .filter((item) => byId.has(item.id))
            .map((item) => [item.id, item.analysis_status]),
        );
        state.pendingAttachments = state.pendingAttachments.map((item) => byId.has(item.id) ? { ...item, ...byId.get(item.id) } : item);
        state.workspaceFiles = state.workspaceFiles.map((item) => byId.has(item.id) ? { ...item, ...byId.get(item.id) } : item);
        const statusChanged = [...byId.values()].some((item) => (
          previousStatus.get(item.id) !== item.analysis_status
        ));
        if (statusChanged) {
          renderAttachmentTray();
          renderFileWorkspace();
        } else {
          updateAudioProgressSurfaces(byId);
        }
        updateSendState();
      }
      await loadOceanListenStatus();
      const activeAudio = [...state.pendingAttachments, ...state.workspaceFiles].some((item) => (
        item?.kind === 'audio' && ['queued', 'running'].includes(item.analysis_status)
      ));
      if (!activeAudio && !state.ocean?.installing) stopOceanPolling();
    } catch (_) {
      // A transient local-network loss should not erase durable backend status.
    } finally {
      state.oceanRefreshing = false;
    }
  }

  function updateAudioProgressSurfaces(byId) {
    byId.forEach((item, id) => {
      const trayRow = dom.attachmentTray?.querySelector(`.pending-attachment[data-id="${cssEscape(id)}"]`);
      const trayNote = trayRow?.querySelector('.pending-file-main small');
      if (trayNote) trayNote.textContent = audioAnalysisLabel(item);
      const progress = trayRow?.querySelector('.pending-audio-progress');
      if (progress) {
        const value = Math.max(0, Math.min(100, Number(item.analysis_progress || 0)));
        progress.style.setProperty('--ocean-progress', `${value}%`);
      }
      [dom.workspaceFileList, dom.workspaceDialogList].forEach((container) => {
        const row = container?.querySelector(`.workspace-file-row[data-id="${cssEscape(id)}"]`);
        const note = row?.querySelector('.workspace-file-copy small');
        if (note) note.textContent = `${formatBytes(item.size || 0)} · ${item.parse_message || audioAnalysisLabel(item)}`;
        const modeNote = row?.querySelector('.workspace-file-copy em');
        if (modeNote && item.analysis_status !== 'ready') modeNote.textContent = audioAnalysisLabel(item);
      });
    });
  }

  async function openFileWorkspaceDialog() {
    await loadFileWorkspace(dom.workspaceDialogSearch?.value || '').catch((error) => alert(error.message));
    if (dom.workspaceDialog?.showModal) dom.workspaceDialog.showModal();
    else dom.workspaceDialog?.setAttribute('open', '');
  }

  function closeFileWorkspaceDialog() {
    if (dom.workspaceDialog?.close) dom.workspaceDialog.close();
    else dom.workspaceDialog?.removeAttribute('open');
  }

  async function loadStickerLibrary() {
    if (dom.stickerMode) dom.stickerMode.value = state.stickerMode;
    try {
      const resp = await fetch('/api/stickers', { cache: 'no-store' });
      const data = await resp.json();
      state.stickers = Array.isArray(data.items) ? data.items : [];
      renderStickerLibrary();
    } catch (e) {
      if (dom.stickerList) dom.stickerList.innerHTML = `<div class="diagnostic-empty">表情包库读取失败：${esc(e.message)}</div>`;
    }
  }

  function renderStickerLibrary() {
    if (dom.stickerCount) dom.stickerCount.textContent = `${state.stickers.length} 张`;
    if (!dom.stickerList) return;
    if (!state.stickers.length) {
      dom.stickerList.innerHTML = '<div class="diagnostic-empty">表情包库还是空的。后续直接点“导入表情包”即可，不需要改代码。</div>';
      return;
    }
    dom.stickerList.innerHTML = state.stickers.map((item) => `
      <div class="sticker-row" data-id="${esc(item.id)}">
        <img src="${esc(item.url || '')}" alt="${esc(item.name || '')}">
        <div><strong>${esc(item.name || '表情包')}</strong><small>${esc((item.tags || []).join('、') || item.description || item.id)}</small></div>
        <button class="btn-soft sticker-edit" type="button">编辑</button>
        <button class="btn-soft sticker-delete" type="button">删除</button>
      </div>`).join('');
    dom.stickerList.querySelectorAll('.sticker-edit').forEach((button) => {
      button.addEventListener('click', () => editSticker(button.closest('.sticker-row')?.dataset.id));
    });
    dom.stickerList.querySelectorAll('.sticker-delete').forEach((button) => {
      button.addEventListener('click', () => deleteSticker(button.closest('.sticker-row')?.dataset.id));
    });
  }

  async function handleStickerImport(event) {
    const file = event.target.files?.[0];
    event.target.value = '';
    if (!file) return;
    const form = new FormData();
    form.append('file', file);
    form.append('name', file.name.replace(/\.[^.]+$/, ''));
    try {
      dom.btnImportSticker.disabled = true;
      dom.btnImportSticker.textContent = '导入中…';
      const resp = await fetch('/api/stickers/import', { method: 'POST', body: form });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.error || data.detail || '导入失败');
      await loadStickerLibrary();
    } catch (e) {
      alert(`表情包导入失败：${e.message}`);
    } finally {
      dom.btnImportSticker.disabled = false;
      dom.btnImportSticker.textContent = '导入表情包';
    }
  }

  async function editSticker(id) {
    const item = state.stickers.find((x) => x.id === id);
    if (!item) return;
    const name = prompt('表情包名称：', item.name || '');
    if (name == null) return;
    const tagsText = prompt('标签（用逗号分隔）：', (item.tags || []).join(','));
    if (tagsText == null) return;
    const description = prompt('使用场景/描述：', item.description || '');
    if (description == null) return;
    const resp = await fetch(`/api/stickers/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name,
        tags: tagsText.replace(/，/g, ',').split(',').map((x) => x.trim()).filter(Boolean),
        description,
      }),
    });
    if (!resp.ok) {
      const data = await resp.json();
      alert(data.detail || data.error || '保存失败');
      return;
    }
    await loadStickerLibrary();
  }

  async function deleteSticker(id) {
    if (!id || !confirm('删除这张本地表情包吗？')) return;
    const resp = await fetch(`/api/stickers/${encodeURIComponent(id)}`, { method: 'DELETE' });
    if (resp.ok) await loadStickerLibrary();
  }

  // ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  //  ElevenLabs 按需朗读
  // ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  function voiceSettingsFromUI() {
    const keyterms = String(dom.voiceKeyterms?.value || '')
      .replace(/，/g, ',')
      .split(/[,\n]/)
      .map((item) => item.trim())
      .filter(Boolean)
      .slice(0, 100);
    return {
      enabled: dom.voiceEnabled?.value === 'true',
      auto_play: dom.voiceAutoPlay?.value === 'true',
      transcript_auto_send: dom.voiceTranscriptAutoSend?.value === 'true',
      call_auto_reply: dom.voiceCallAutoReply?.value !== 'false',
      voice_id: dom.voiceId?.value.trim() || '',
      model_id: dom.voiceModel?.value || 'eleven_v3',
      language_code: 'zh',
      stability: Number(dom.voiceStability?.value || 0.36),
      similarity_boost: Number(dom.voiceSimilarity?.value || 0.75),
      style: Number(dom.voiceStyle?.value || 0.72),
      speed: Number(dom.voiceSpeed?.value || 1.08),
      use_speaker_boost: true,
      delivery_tag: dom.voiceDelivery?.value || 'auto',
      keyterms,
      greeting_text: dom.voiceGreetingText?.value.trim() || '嗯，我在。听得到吗？',
      native_language: 'zh',
      translation_enabled: dom.voiceTranslationEnabled?.value !== 'false',
      mood_enabled: dom.voiceMoodEnabled?.value !== 'false',
      post_eq: dom.voicePostEq?.value !== 'false',
    };
  }

  function setVoiceNote(message, tone = '') {
    if (!dom.voiceSettingsNote) return;
    dom.voiceSettingsNote.textContent = message;
    dom.voiceSettingsNote.dataset.tone = tone;
  }

  function detectVoiceCapabilities() {
    const localhost = ['localhost', '127.0.0.1', '::1'].includes(location.hostname);
    const secure = Boolean(window.isSecureContext || localhost);
    return {
      secure,
      mediaRecorder: Boolean(
        secure && navigator.mediaDevices?.getUserMedia && window.MediaRecorder
      ),
      dictation: Boolean(window.SpeechRecognition || window.webkitSpeechRecognition),
      speechSynthesis: Boolean(
        window.speechSynthesis && window.SpeechSynthesisUtterance
      ),
      ios: /iPad|iPhone|iPod/.test(navigator.userAgent)
        || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1),
    };
  }

  function renderVoiceSettings(data) {
    if (!data || typeof data !== 'object') return;
    const capabilities = detectVoiceCapabilities();
    data.browser_capabilities = capabilities;
    data.browser_fallback = Boolean(
      capabilities.dictation || capabilities.speechSynthesis
    );
    state.voice = data;
    const settings = data.settings || {};
    if (dom.voiceEnabled) dom.voiceEnabled.value = String(settings.enabled === true);
    if (dom.voiceAutoPlay) dom.voiceAutoPlay.value = String(settings.auto_play === true);
    if (dom.voiceTranscriptAutoSend) {
      dom.voiceTranscriptAutoSend.value = String(settings.transcript_auto_send === true);
    }
    if (dom.voiceCallAutoReply) {
      dom.voiceCallAutoReply.value = String(settings.call_auto_reply !== false);
    }
    if (dom.voiceId) dom.voiceId.value = settings.voice_id || '';
    if (dom.voiceModel) dom.voiceModel.value = settings.model_id || 'eleven_v3';
    if (dom.voiceDelivery) dom.voiceDelivery.value = settings.delivery_tag || 'auto';
    if (dom.voiceKeyterms) {
      dom.voiceKeyterms.value = Array.isArray(settings.keyterms)
        ? settings.keyterms.join('，') : '';
    }
    if (dom.voiceGreetingText) {
      dom.voiceGreetingText.value = settings.greeting_text || '嗯，我在。听得到吗？';
    }
    if (dom.voiceTranslationEnabled) {
      dom.voiceTranslationEnabled.value = String(settings.translation_enabled !== false);
    }
    if (dom.voiceMoodEnabled) {
      dom.voiceMoodEnabled.value = String(settings.mood_enabled !== false);
    }
    if (dom.voicePostEq) {
      dom.voicePostEq.value = String(settings.post_eq !== false);
    }
    [
      [dom.voiceStability, dom.voiceStabilityValue, settings.stability, 0.36],
      [dom.voiceSimilarity, dom.voiceSimilarityValue, settings.similarity_boost, 0.75],
      [dom.voiceStyle, dom.voiceStyleValue, settings.style, 0.72],
      [dom.voiceSpeed, dom.voiceSpeedValue, settings.speed, 1.08],
    ].forEach(([input, output, value, fallback]) => {
      const numeric = Number.isFinite(Number(value)) ? Number(value) : fallback;
      if (input) input.value = String(numeric);
      if (output) output.textContent = numeric.toFixed(2);
    });

    if (dom.voiceReadyBadge) {
      const enabled = settings.enabled === true;
      const inputReady = Boolean(
        (data.stt_ready && capabilities.mediaRecorder) || capabilities.dictation
      );
      const label = data.tts_ready && inputReady ? '语音与朗读已就绪'
        : !enabled ? '当前关闭'
          : !capabilities.secure ? '需要 HTTPS'
            : !inputReady ? '此浏览器无法录音'
              : !data.key_configured ? '浏览器听写备用'
            : data.stt_ready && !data.voice_configured ? '转写已就绪'
              : '尚未就绪';
      dom.voiceReadyBadge.textContent = label;
      dom.voiceReadyBadge.className = `voice-ready-badge ${((data.tts_ready || data.stt_ready) && inputReady) ? 'ready' : (enabled ? 'missing' : 'off')}`;
    }
    if (dom.btnRefreshVoices) dom.btnRefreshVoices.disabled = !data.key_configured;
    updateVoiceSpeedSupport();

    if (!capabilities.secure && capabilities.ios) {
      setVoiceNote('iPhone 当前通过普通 HTTP 打开，Safari 不会稳定开放麦克风。请改用 HTTPS 地址；文字聊天仍可正常使用。', 'warn');
    } else if (!capabilities.mediaRecorder && !capabilities.dictation) {
      setVoiceNote('这个浏览器没有检测到可用的录音或听写接口。可以继续打字；若要语音，请换最新版 Safari/Chrome 并使用 HTTPS。', 'warn');
    } else if (!data.key_configured) {
      setVoiceNote('没有 Key 时会尝试浏览器自带听写与系统声音；效果和兼容性由手机/浏览器决定。想要稳定音色与转写，请在 .env 填入 ELEVENLABS_API_KEY。', 'quiet');
    } else if (!data.voice_configured) {
      setVoiceNote('Scribe 语音转写已经可用。点击“读取音色”或粘贴 Voice ID 后，AI 回复也能自动播放。', 'warn');
    } else if (!settings.enabled) {
      setVoiceNote('接口已配置，但语音开关关闭；不会产生语音费用。', 'quiet');
    } else {
      setVoiceNote(`语音消息与陪伴通话已就绪。朗读单次最多 ${Number(data.max_chars || 5000).toLocaleString()} 字，默认按需生成。`, 'ok');
    }
  }

  async function loadVoiceSettings() {
    try {
      const data = await fetchJSON('/api/voice/settings', { cache: 'no-store' });
      renderVoiceSettings(data);
      try {
        const selectedVoice = String(data?.settings?.voice_id || dom.voiceId?.value || '').trim();
        if (selectedVoice) localStorage.setItem('daxigua:voice-id', selectedVoice);
        else localStorage.removeItem('daxigua:voice-id');
      } catch (_) {}
      return data;
    } catch (error) {
      state.voice = null;
      if (dom.voiceReadyBadge) {
        dom.voiceReadyBadge.textContent = '状态读取失败';
        dom.voiceReadyBadge.className = 'voice-ready-badge missing';
      }
      setVoiceNote(`语音设置没有接上：${error.message}`, 'warn');
      return null;
    }
  }

  function updateVoiceSpeedSupport() {
    const isV3 = (dom.voiceModel?.value || 'eleven_v3') === 'eleven_v3';
    if (dom.voiceSpeed) dom.voiceSpeed.disabled = isV3;
    dom.voiceSpeedWrap?.classList.toggle('is-disabled', isV3);
    if (dom.voiceSpeedNote) {
      dom.voiceSpeedNote.textContent = isV3
        ? 'Eleven v3 当前不接收 speed；这里的数值会保留，但请求时自动省略。'
        : '该模型支持 0.70–1.20 倍语速。';
    }
  }

  async function saveVoiceSettings({ silent = false } = {}) {
    const original = dom.btnSaveVoice?.textContent || '保存设置';
    try {
      if (dom.btnSaveVoice) {
        dom.btnSaveVoice.disabled = true;
        dom.btnSaveVoice.textContent = '保存中…';
      }
      const data = await fetchJSON('/api/voice/settings', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(voiceSettingsFromUI()),
      });
      renderVoiceSettings(data);
      try {
        const selectedVoice = String(data?.settings?.voice_id || '').trim();
        if (selectedVoice) localStorage.setItem('daxigua:voice-id', selectedVoice);
        else localStorage.removeItem('daxigua:voice-id');
      } catch (_) {}
      const voiceReady = Boolean(data.tts_ready || data.stt_ready || data.browser_fallback);
      if (!silent) {
        setVoiceNote(
          voiceReady ? '语音设置已保存。可发送语音消息；配置音色后也能朗读回复。' : '语音设置已保存；按上方提示补齐配置后即可使用。',
          voiceReady ? 'ok' : 'quiet',
        );
      }
      return data;
    } catch (error) {
      setVoiceNote(`保存失败：${error.message}`, 'warn');
      if (!silent) alert(`语音设置保存失败：${error.message}`);
      throw error;
    } finally {
      if (dom.btnSaveVoice) {
        dom.btnSaveVoice.disabled = false;
        dom.btnSaveVoice.textContent = original;
      }
    }
  }

  async function refreshVoiceOptions() {
    const original = dom.btnRefreshVoices?.textContent || '读取音色';
    try {
      if (dom.btnRefreshVoices) {
        dom.btnRefreshVoices.disabled = true;
        dom.btnRefreshVoices.textContent = '读取中…';
      }
      setVoiceNote('正在从 ElevenLabs 读取可用音色；这一步不会生成语音。', 'quiet');
      const data = await fetchJSON('/api/voice/voices', { cache: 'no-store' });
      const voices = Array.isArray(data.voices) ? data.voices : [];
      if (dom.voiceOptions) {
        dom.voiceOptions.innerHTML = voices.map((voice) => {
          const suffix = voice.category ? ` · ${voice.category}` : '';
          return `<option value="${esc(voice.voice_id)}">${esc(voice.name || voice.voice_id)}${esc(suffix)}</option>`;
        }).join('');
      }
      setVoiceNote(`已读取 ${voices.length} 个音色。选中或粘贴 Voice ID 后记得保存。`, 'ok');
    } catch (error) {
      setVoiceNote(`音色读取失败：${error.message}`, 'warn');
      alert(`音色读取失败：${error.message}`);
    } finally {
      if (dom.btnRefreshVoices) {
        dom.btnRefreshVoices.disabled = !(state.voice?.key_configured);
        dom.btnRefreshVoices.textContent = original;
      }
    }
  }

  async function voiceFetch(url, options = {}, timeoutMs = 100000) {
    const controller = new AbortController();
    const upstreamSignal = options.signal;
    const relayAbort = () => controller.abort();
    upstreamSignal?.addEventListener?.('abort', relayAbort, { once: true });
    const timeoutId = window.setTimeout(() => controller.abort(), timeoutMs);
    try {
      return await fetch(url, { ...options, signal: controller.signal });
    } catch (error) {
      if (error?.name === 'AbortError' && !upstreamSignal?.aborted) {
        throw new Error('语音请求等待超时，请检查网络后重试');
      }
      throw error;
    } finally {
      window.clearTimeout(timeoutId);
      upstreamSignal?.removeEventListener?.('abort', relayAbort);
    }
  }

  async function requestVoiceBlob(text) {
    let preferredVoice = '';
    try { preferredVoice = localStorage.getItem('daxigua:voice-id') || ''; } catch (_) {}
    const response = await voiceFetch('/api/voice/synthesize', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: String(text || ''), voice_id: preferredVoice }),
    }, 100000);
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.detail || data.error || `语音生成失败 (${response.status})`);
    }
    return response.blob();
  }

  function stopActiveVoice(except = null) {
    const active = state.activeVoiceAudio;
    if (active && active !== except && !active.paused) active.pause();
    if (except) state.activeVoiceAudio = except;
  }

  function releaseVoiceAudio() {
    stopActiveVoice();
    state.activeVoiceAudio = null;
    dom.messages?.querySelectorAll('audio[data-voice-url]').forEach((audio) => {
      audio.pause();
      const url = audio.dataset.voiceUrl;
      if (url) URL.revokeObjectURL(url);
      audio.removeAttribute('data-voice-url');
    });
  }

  function wireMessageVoicePlayer(audio, button) {
    audio.addEventListener('play', () => {
      stopActiveVoice(audio);
      if (button) button.textContent = '暂停';
    });
    audio.addEventListener('pause', () => {
      if (button && !audio.ended) button.textContent = '播放';
    });
    audio.addEventListener('ended', () => {
      if (button) button.textContent = '再听一次';
      if (state.activeVoiceAudio === audio) state.activeVoiceAudio = null;
    });
  }

  async function speakMessage(messageEl, { silent = false, autoplay = false } = {}) {
    if (!messageEl || messageEl.dataset.role !== 'assistant') return;
    const button = messageEl.querySelector('[data-message-action="speak"]');
    const existing = messageEl.querySelector('.message-voice-player');
    if (existing?.src) {
      if (existing.paused) await existing.play().catch(() => {});
      else existing.pause();
      return;
    }

    if (!state.voice) await loadVoiceSettings();
    if (!state.voice?.ready) {
      const reason = !state.voice?.key_configured ? '还没有配置 ElevenLabs Key。'
        : !state.voice?.voice_configured ? '还没有选择 Voice ID。'
          : '语音通道目前关闭。';
      if (!silent) alert(`${reason}\n请到“系统 → 语音与朗读”完成设置。`);
      return;
    }
    const text = String(messageEl._rawText || messageEl.querySelector('.bubble')?.innerText || '').trim();
    if (!text) return;
    const original = button?.textContent || '朗读';
    try {
      if (button) {
        button.disabled = true;
        button.textContent = '生成语音…';
      }
      const blob = await requestVoiceBlob(text);
      const url = URL.createObjectURL(blob);
      const audio = document.createElement('audio');
      audio.className = 'message-voice-player';
      audio.controls = true;
      audio.preload = 'metadata';
      audio.src = url;
      audio.dataset.voiceUrl = url;
      messageEl.querySelector('.message-media')?.appendChild(audio);
      wireMessageVoicePlayer(audio, button);
      if (button) button.disabled = false;
      if (autoplay || !silent) await audio.play().catch(() => {});
      if (button && audio.paused) button.textContent = '播放';
    } catch (error) {
      if (button) button.textContent = original;
      if (!silent) alert(`朗读失败：${error.message}`);
      else console.warn('自动朗读失败:', error);
    } finally {
      if (button) button.disabled = false;
    }
  }

  async function testVoice() {
    const original = dom.btnTestVoice?.textContent || '试听一次';
    try {
      if (dom.btnTestVoice) {
        dom.btnTestVoice.disabled = true;
        dom.btnTestVoice.textContent = '生成中…';
      }
      const settings = await saveVoiceSettings({ silent: true });
      if (!settings?.ready) throw new Error('请先开启语音，并配置 Key 与 Voice ID');
      setVoiceNote('正在生成一次试听；这次会消耗 ElevenLabs 额度。', 'warn');
      const blob = await requestVoiceBlob(dom.voiceTestText?.value || '嗯，我在。听得到吗？');
      if (dom.voicePreview) {
        const oldUrl = dom.voicePreview.dataset.voiceUrl;
        if (oldUrl) URL.revokeObjectURL(oldUrl);
        const url = URL.createObjectURL(blob);
        dom.voicePreview.src = url;
        dom.voicePreview.dataset.voiceUrl = url;
        dom.voicePreview.classList.remove('hidden');
        stopActiveVoice(dom.voicePreview);
        await dom.voicePreview.play().catch(() => {});
      }
      setVoiceNote('试听已生成。满意后，聊天回复下方的“朗读”会使用同一套设置。', 'ok');
    } catch (error) {
      setVoiceNote(`试听失败：${error.message}`, 'warn');
      alert(`试听失败：${error.message}`);
    } finally {
      if (dom.btnTestVoice) {
        dom.btnTestVoice.disabled = false;
        dom.btnTestVoice.textContent = original;
      }
    }
  }

  async function generateVoiceGreeting() {
    const original = dom.btnGenerateGreeting?.textContent || '预生成问候';
    try {
      if (dom.btnGenerateGreeting) {
        dom.btnGenerateGreeting.disabled = true;
        dom.btnGenerateGreeting.textContent = '正在生成…';
      }
      const settings = await saveVoiceSettings({ silent: true });
      if (!settings?.tts_ready) throw new Error('请先开启语音并配置 Voice ID');
      const response = await voiceFetch('/api/voice/greeting', { method: 'POST' }, 100000);
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || '问候生成失败');
      setVoiceNote('本地通话问候已经缓存；接通时会立即播放，不必等待第一轮模型回复。', 'ok');
    } catch (error) {
      setVoiceNote(`问候生成失败：${error.message}`, 'warn');
    } finally {
      if (dom.btnGenerateGreeting) {
        dom.btnGenerateGreeting.disabled = false;
        dom.btnGenerateGreeting.textContent = original;
      }
    }
  }

  // ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  //  v7.0.1 语音消息与半双工陪伴通话
  // ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  function setVoiceCaptureStatus(message = '', tone = '') {
    if (!dom.voiceCaptureStatus) return;
    dom.voiceCaptureStatus.textContent = message;
    dom.voiceCaptureStatus.dataset.tone = tone;
    dom.voiceCaptureStatus.classList.toggle('hidden', !message);
  }

  function setVoiceCallState(value, label) {
    if (dom.voiceCallDialog) dom.voiceCallDialog.dataset.state = value || 'idle';
    if (dom.voiceCallStatus && label) dom.voiceCallStatus.textContent = label;
    if (dom.voiceCallTalk) {
      const busy = ['transcribing', 'thinking', 'speaking'].includes(value);
      dom.voiceCallTalk.disabled = busy;
      dom.voiceCallTalk.classList.toggle('is-recording', value === 'recording');
      const title = dom.voiceCallTalk.querySelector('span');
      const hint = dom.voiceCallTalk.querySelector('small');
      if (title) title.textContent = value === 'recording' ? '正在听你说' : (busy ? '请稍等' : '按住说话');
      if (hint) hint.textContent = value === 'recording' ? '松开发送' : (busy ? label || '处理中' : '松开发送');
    }
  }

  function releaseVoiceCaptureUI() {
    dom.btnVoiceMessage?.classList.remove('is-recording');
    if (!state.voiceCallActive) setVoiceCaptureStatus('');
    if (dom.voiceCallTalk) dom.voiceCallTalk.classList.remove('is-recording');
  }

  function recorderMimeType() {
    if (!window.MediaRecorder) return '';
    const choices = [
      'audio/webm;codecs=opus',
      'audio/mp4;codecs=mp4a.40.2',
      'audio/webm',
      'audio/mp4',
      'audio/ogg;codecs=opus',
    ];
    return choices.find((type) => {
      try { return MediaRecorder.isTypeSupported(type); } catch (_) { return false; }
    }) || '';
  }

  function captureUiStarted(mode) {
    if (mode === 'message') {
      dom.btnVoiceMessage?.classList.add('is-recording');
      setVoiceCaptureStatus('正在录音，再点一次麦克风就结束并转成文字。', 'recording');
    } else {
      setVoiceCallState('recording', '正在听你说…松开后发送');
    }
  }

  function captureFailed(message, mode) {
    const capture = state.voiceCapture;
    if (capture) {
      capture.cancelled = true;
      capture.failed = true;
      if (capture.type === 'media') {
        capture.stream?.getTracks?.().forEach((track) => track.stop());
        try {
          if (capture.recorder?.state !== 'inactive') capture.recorder.stop();
        } catch (_) {}
      } else if (capture.type === 'recognition') {
        try { capture.recognition.abort(); } catch (_) {}
      }
      if (state.voiceCapture === capture) state.voiceCapture = null;
    }
    releaseVoiceCaptureUI();
    if (mode === 'call') setVoiceCallState('idle', message);
    else setVoiceCaptureStatus(message, 'error');
  }

  async function startMediaRecorderCapture(mode, pending) {
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
        channelCount: 1,
      },
    });
    if (pending.cancelled || state.voiceCapture !== pending) {
      stream.getTracks().forEach((track) => track.stop());
      return;
    }
    const mimeType = recorderMimeType();
    const recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
    const capture = {
      type: 'media',
      mode,
      recorder,
      stream,
      chunks: [],
      startedAt: performance.now(),
      mimeType: recorder.mimeType || mimeType || 'audio/webm',
    };
    state.voiceCapture = capture;
    recorder.addEventListener('dataavailable', (event) => {
      if (event.data?.size) capture.chunks.push(event.data);
    });
    recorder.addEventListener('error', () => {
      captureFailed('录音设备发生错误，请重新授权后再试。', mode);
    });
    recorder.addEventListener('stop', () => {
      capture.stream.getTracks().forEach((track) => track.stop());
      if (state.voiceCapture === capture) state.voiceCapture = null;
      releaseVoiceCaptureUI();
      if (capture.failed || capture.cancelled) return;
      const durationMs = Math.max(0, Math.round(performance.now() - capture.startedAt));
      const blob = new Blob(capture.chunks, { type: capture.mimeType });
      handleRecordedVoice(blob, mode, durationMs).catch((error) => {
        captureFailed(`语音处理失败：${error.message}`, mode);
      });
    }, { once: true });
    recorder.start(250);
    captureUiStarted(mode);
  }

  function startBrowserDictation(mode, pending) {
    const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!Recognition) throw new Error('这个浏览器没有可用的录音转写能力');
    const recognition = new Recognition();
    recognition.lang = 'zh-CN';
    recognition.continuous = false;
    recognition.interimResults = true;
    let finalText = '';
    let interimText = '';
    const capture = {
      type: 'recognition',
      mode,
      recognition,
      startedAt: performance.now(),
      cancelled: false,
    };
    if (pending.cancelled || state.voiceCapture !== pending) return;
    state.voiceCapture = capture;
    recognition.addEventListener('result', (event) => {
      interimText = '';
      for (let i = event.resultIndex; i < event.results.length; i += 1) {
        const chunk = event.results[i][0]?.transcript || '';
        if (event.results[i].isFinal) finalText += chunk;
        else interimText += chunk;
      }
      const preview = `${finalText}${interimText}`.trim();
      if (mode === 'call') setVoiceCallState('recording', preview || '正在听你说…');
      else setVoiceCaptureStatus(preview ? `听到：${preview}` : '正在听你说…', 'recording');
    });
    recognition.addEventListener('error', (event) => {
      if (event.error !== 'aborted') {
        capture.cancelled = true;
        captureFailed(`浏览器听写失败：${event.error || '未知错误'}`, mode);
      }
    });
    recognition.addEventListener('end', () => {
      if (state.voiceCapture === capture) state.voiceCapture = null;
      releaseVoiceCaptureUI();
      const text = `${finalText}${interimText}`.trim();
      if (text && !capture.cancelled) {
        processVoiceTranscript(text, mode, {
          durationMs: Math.max(0, Math.round(performance.now() - capture.startedAt)),
          transcriber: 'browser',
        }).catch((error) => captureFailed(`发送失败：${error.message}`, mode));
      } else if (!capture.cancelled) {
        captureFailed('没有听清楚，再说一次就好。', mode);
      }
    }, { once: true });
    recognition.start();
    captureUiStarted(mode);
  }

  async function startVoiceCapture(mode = 'message') {
    if (state.voiceCapture || state.isStreaming) {
      if (state.isStreaming && mode === 'message') {
        setVoiceCaptureStatus('先等这一轮回复结束，再录下一条语音。', 'quiet');
      }
      return;
    }
    if (mode === 'call' && !state.voiceCallActive) return;
    if (!state.voice) await loadVoiceSettings();
    const capabilities = detectVoiceCapabilities();
    const pending = { type: 'pending', mode, cancelled: false };
    state.voiceCapture = pending;
    try {
      if (!capabilities.secure && capabilities.ios) {
        throw new Error('iPhone 麦克风需要 HTTPS 地址；当前普通 HTTP 只能使用文字聊天');
      }
      const canUpload = Boolean(
        state.voice?.stt_ready && capabilities.mediaRecorder
      );
      if (canUpload) await startMediaRecorderCapture(mode, pending);
      else if (capabilities.dictation) startBrowserDictation(mode, pending);
      else if (!capabilities.secure) {
        throw new Error('当前地址不是安全连接，浏览器没有开放麦克风；请使用 HTTPS');
      } else {
        throw new Error('这个浏览器没有可用的录音转写能力');
      }
    } catch (error) {
      if (state.voiceCapture === pending) state.voiceCapture = null;
      captureFailed(
        error?.name === 'NotAllowedError'
          ? '没有麦克风权限。请在浏览器设置里允许后重试。'
          : `语音没有启动：${error.message}`,
        mode,
      );
    }
  }

  function stopVoiceCapture() {
    const capture = state.voiceCapture;
    if (!capture) return;
    if (capture.type === 'pending') {
      capture.cancelled = true;
      state.voiceCapture = null;
      releaseVoiceCaptureUI();
      return;
    }
    if (capture.type === 'media') {
      if (capture.recorder.state !== 'inactive') capture.recorder.stop();
      return;
    }
    if (capture.type === 'recognition') {
      try { capture.recognition.stop(); } catch (_) {}
    }
  }

  function toggleVoiceMessageCapture() {
    if (state.voiceCapture?.mode === 'message') stopVoiceCapture();
    else startVoiceCapture('message');
  }

  async function handleRecordedVoice(blob, mode, durationMs) {
    if (!blob.size) throw new Error('录音内容是空的');
    if (mode === 'call') setVoiceCallState('transcribing', '正在把你的声音变成文字…');
    else setVoiceCaptureStatus('正在转成文字…', 'working');
    const extension = blob.type.includes('mp4') ? 'm4a'
      : blob.type.includes('ogg') ? 'ogg' : 'webm';
    const form = new FormData();
    form.append('file', blob, `voice-${Date.now()}.${extension}`);
    form.append('language_code', 'zh');
    if (mode === 'call' && state.voiceCallId) form.append('call_id', state.voiceCallId);
    const response = await voiceFetch(
      '/api/voice/transcribe', { method: 'POST', body: form }, 120000
    );
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.detail || data.error || `转写失败 (${response.status})`);
    }
    const data = await response.json();
    const transcript = String(data.text || '').trim();
    if (data.human_signal === false) {
      const eventText = Array.isArray(data.audio_events) && data.audio_events.length
        ? `只听到：${data.audio_events.join('、')}` : '没有检测到稳定真人说话';
      throw new Error(eventText);
    }
    if (!transcript) throw new Error('没有识别到可发送的文字');
    await processVoiceTranscript(transcript, mode, {
      durationMs,
      transcriber: data.provider || 'ElevenLabs',
      acoustic: data.acoustic || {},
      mood: data.mood || {},
    });
  }

  function appendVoiceSubtitle(role, text) {
    if (!dom.voiceCallSubtitles || !text) return;
    dom.voiceCallSubtitles.querySelector('.voice-call-empty')?.remove();
    const item = document.createElement('div');
    item.className = `voice-subtitle ${role}`;
    item.innerHTML = `<small>${role === 'user' ? '你' : esc(companionName)} · ${new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</small><span>${esc(text)}</span>`;
    dom.voiceCallSubtitles.appendChild(item);
    dom.voiceCallSubtitles.scrollTop = dom.voiceCallSubtitles.scrollHeight;
  }

  async function processVoiceTranscript(text, mode, details = {}) {
    const transcript = String(text || '').trim();
    if (!transcript) return;
    if (mode === 'message') {
      dom.input.value = transcript;
      autoResize(dom.input);
      updateSendState();
      setVoiceCaptureStatus(`已转成文字：${transcript}`, 'ok');
      if (state.voice?.settings?.transcript_auto_send) {
        await sendMessage({
          text: transcript,
          voiceTranscript: true,
          voiceDurationMs: details.durationMs,
          voiceTranscriber: details.transcriber,
          voiceAcoustic: details.acoustic,
          voiceMood: details.mood,
        });
        setVoiceCaptureStatus('');
      } else {
        dom.input.focus();
      }
      return;
    }
    if (!state.voiceCallActive) return;
    appendVoiceSubtitle('user', transcript);
    setVoiceCallState('thinking', `${companionName}正在想怎么回答…`);
    const result = await sendMessage({
      text: transcript,
      voiceTranscript: true,
      voiceDurationMs: details.durationMs,
      voiceTranscriber: details.transcriber,
      voiceAcoustic: details.acoustic,
      voiceMood: details.mood,
      suppressAutoVoice: true,
    });
    if (!state.voiceCallActive) return;
    const reply = String(result?.text || result?.messageEl?._rawText || '').trim();
    if (!reply) {
      setVoiceCallState('idle', '这次没有收到文字回复，可以再说一次。');
      return;
    }
    appendVoiceSubtitle('assistant', reply);
    if (state.voiceCallId) {
      fetch(`/api/voice/calls/${encodeURIComponent(state.voiceCallId)}/turn`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ role: 'assistant' }),
      }).catch(() => {});
    }
    if (state.voice?.settings?.call_auto_reply !== false) {
      await playVoiceCallReply(reply);
    }
    if (state.voiceCallActive) setVoiceCallState('idle', '线路在线 · 按住说话');
  }

  function browserSpokenText(text) {
    return String(text || '')
      .replace(/```[\s\S]*?```/g, '。这里省略了一段代码。')
      .replace(/`([^`]+)`/g, '$1')
      .replace(/!\[([^\]]*)\]\([^)]+\)/g, '$1')
      .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1')
      .replace(/^#{1,6}\s+/gm, '')
      .replace(/[*_~>|]/g, '')
      .replace(/\n{2,}/g, '。')
      .replace(/\s+/g, ' ')
      .trim();
  }

  function speakWithBrowser(text) {
    return new Promise((resolve, reject) => {
      if (!window.speechSynthesis || !window.SpeechSynthesisUtterance) {
        reject(new Error('浏览器没有系统朗读能力'));
        return;
      }
      window.speechSynthesis.cancel();
      const utterance = new SpeechSynthesisUtterance(browserSpokenText(text));
      let settled = false;
      const finish = (callback, value) => {
        if (settled) return;
        settled = true;
        window.clearTimeout(timeoutId);
        callback(value);
      };
      const timeoutId = window.setTimeout(() => {
        window.speechSynthesis.cancel();
        finish(reject, new Error('系统朗读等待超时'));
      }, 45000);
      utterance.lang = 'zh-CN';
      utterance.rate = 1;
      utterance.onend = () => finish(resolve);
      utterance.onerror = (event) => finish(
        reject, new Error(event.error || '系统朗读失败')
      );
      window.speechSynthesis.speak(utterance);
    });
  }

  async function playVoiceCallReply(text) {
    if (!state.voiceCallActive) return;
    setVoiceCallState('speaking', `${companionName}正在说话…`);
    if (state.voice?.tts_ready) {
      try {
        const blob = await requestVoiceBlob(text);
        if (!state.voiceCallActive) return;
        const url = URL.createObjectURL(blob);
        const audio = dom.voiceCallAudio;
        if (!audio) throw new Error('通话播放器没有准备好');
        const previous = audio.dataset.voiceUrl;
        if (previous) URL.revokeObjectURL(previous);
        audio.src = url;
        audio.dataset.voiceUrl = url;
        stopActiveVoice(audio);
        await new Promise((resolve, reject) => {
          let settled = false;
          const finish = (callback, value) => {
            if (settled) return;
            settled = true;
            audio.onended = null;
            audio.onerror = null;
            audio.onpause = null;
            callback(value);
          };
          audio.onended = () => finish(resolve);
          // A second player or hang-up pauses this element. Treat that as a
          // deliberate interruption so the call never remains "speaking".
          audio.onpause = () => finish(resolve);
          audio.onerror = () => finish(reject, new Error('语音播放失败'));
          audio.play().catch((error) => finish(reject, error));
        });
        return;
      } catch (error) {
        console.warn('ElevenLabs 通话朗读失败，尝试系统声音:', error);
      }
    }
    try {
      await speakWithBrowser(text);
    } catch (error) {
      setVoiceCallState('idle', `字幕已显示；声音不可用：${error.message}`);
    }
  }

  async function openVoiceCall() {
    if (state.voiceCallActive) {
      if (dom.voiceCallDialog && !dom.voiceCallDialog.open) {
        dom.voiceCallDialog.showModal();
      }
      return;
    }
    if (!state.currentSession) newSession(false);
    if (!state.voice) await loadVoiceSettings();
    try {
      const call = await fetchJSON('/api/voice/calls', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: state.currentSession }),
      });
      state.voiceCallId = call.id;
      state.voiceCallActive = true;
      if (dom.voiceCallSubtitles) {
        dom.voiceCallSubtitles.innerHTML = '<div class="voice-call-empty">按住下方按钮说话；松开后会自动转成文字。</div>';
      }
      const capabilities = detectVoiceCapabilities();
      const uploadReady = Boolean(state.voice?.stt_ready && capabilities.mediaRecorder);
      const inputReady = uploadReady || capabilities.dictation;
      setVoiceCallState('idle', uploadReady
        ? 'Scribe 线路在线 · 按住说话'
        : capabilities.dictation
          ? '浏览器听写线路 · 按住说话'
          : capabilities.secure
            ? '当前浏览器没有语音输入能力'
            : '请用 HTTPS 打开后再使用麦克风');
      if (!inputReady) dom.voiceCallTalk.disabled = true;
      if (dom.voiceCallDialog && !dom.voiceCallDialog.open) dom.voiceCallDialog.showModal();
      try { window.DaxiguaVoice?.startVoiceKeepAlive?.(state.voiceCallId || ''); } catch (_) {}
    } catch (error) {
      alert(`陪伴通话没有启动：${error.message}`);
    }
  }

  async function endVoiceCall() {
    if (!state.voiceCallActive && !dom.voiceCallDialog?.open) return;
    const callId = state.voiceCallId;
    state.voiceCallActive = false;
    state.voiceCallId = null;
    stopVoiceCapture();
    window.speechSynthesis?.cancel?.();
    if (dom.voiceCallAudio) {
      dom.voiceCallAudio.pause();
      const url = dom.voiceCallAudio.dataset.voiceUrl;
      if (url) URL.revokeObjectURL(url);
      dom.voiceCallAudio.removeAttribute('src');
      dom.voiceCallAudio.removeAttribute('data-voice-url');
    }
    if (dom.voiceCallDialog?.open) dom.voiceCallDialog.close();
    try { window.DaxiguaVoice?.stopVoiceKeepAlive?.(callId || ''); } catch (_) {}
    if (callId) {
      fetch(`/api/voice/calls/${encodeURIComponent(callId)}`, {
        method: 'DELETE',
        keepalive: true,
      })
        .catch(() => {});
    }
  }

  function finishVoiceCallOnPageExit() {
    const callId = state.voiceCallId;
    const capture = state.voiceCapture;
    if (capture?.type === 'media') {
      capture.stream?.getTracks?.().forEach((track) => track.stop());
    } else if (capture?.type === 'recognition') {
      try { capture.recognition.abort(); } catch (_) {}
    }
    window.speechSynthesis?.cancel?.();
    if (!callId) return;
    state.voiceCallId = null;
    state.voiceCallActive = false;
    const endpoint = `/api/voice/calls/${encodeURIComponent(callId)}/end`;
    try {
      if (navigator.sendBeacon) {
        navigator.sendBeacon(endpoint, new Blob([], { type: 'text/plain' }));
      } else {
        fetch(endpoint, { method: 'POST', keepalive: true }).catch(() => {});
      }
    } catch (_) {}
  }

  function toggleVoiceRoute() {
    state.voiceRoute = state.voiceRoute === 'speaker' ? 'earpiece' : 'speaker';
    const label = state.voiceRoute === 'speaker' ? '扬声器' : '听筒';
    if (dom.voiceCallRoute) dom.voiceCallRoute.textContent = label;
    try {
      if (window.DaxiguaVoice?.setAudioRoute) {
        window.DaxiguaVoice.setAudioRoute(state.voiceRoute);
      } else {
        setVoiceCallState('idle', `网页端由系统管理输出；Android 壳中会切换到${label}。`);
      }
    } catch (error) {
      setVoiceCallState('idle', `输出切换失败：${error.message}`);
    }
  }

  // ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  //  控制台
  // ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  async function loadRelationalHonestyData() {
    const list = $('#honesty-audits');
    try {
      const [statusResponse, auditsResponse] = await Promise.all([
        fetch('/api/relational-honesty/status', { cache: 'no-store' }),
        fetch('/api/relational-honesty/audits?limit=30', { cache: 'no-store' }),
      ]);
      const status = await statusResponse.json();
      const audits = await auditsResponse.json();
      if (!statusResponse.ok || !auditsResponse.ok) {
        throw new Error(status.detail || audits.detail || '关系诚实状态读取失败');
      }
      if (dom.honestyEnabled) dom.honestyEnabled.value = status.enabled ? 'true' : 'false';
      $('#honesty-mode').textContent = status.mode === 'strict_pre_send' ? '严格发送前' : '未启用';
      $('#honesty-audit-count').textContent = Number(status.audit_count || 0).toLocaleString();
      $('#honesty-rewritten-count').textContent = Number(status.rewritten_count || 0).toLocaleString();
      $('#honesty-stores-text').textContent = status.stores_draft_text ? '是' : '否';
      const items = Array.isArray(audits.items) ? audits.items : [];
      if (list) {
        list.innerHTML = items.length ? items.map((item) => {
          const categories = Array.isArray(item.categories) && item.categories.length
            ? item.categories.join(' · ') : '未命中';
          return `<div class="diagnostic-row">
            <span>${esc(item.action || 'checked')}</span>
            <small>${esc(categories)} · 摘要 ${esc(item.draft_sha256 || '')}</small>
            <em>${esc(item.created_at || '')}</em>
          </div>`;
        }).join('') : '<div class="diagnostic-empty">还没有审计记录</div>';
      }
    } catch (error) {
      if (list) list.innerHTML = `<div class="diagnostic-empty">读取失败：${esc(error.message)}</div>`;
    }
  }

  async function saveRelationalHonestySettings() {
    try {
      const status = await fetchJSON('/api/relational-honesty/settings', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: dom.honestyEnabled?.value === 'true' }),
      });
      if (dom.honestyEnabled) dom.honestyEnabled.value = status.enabled ? 'true' : 'false';
      await loadRelationalHonestyData();
    } catch (error) {
      alert(`关系诚实设置保存失败：${error.message}`);
      await loadRelationalHonestyData();
    }
  }

  function formatUsageCost(value) {
    const amount = Number(value || 0);
    if (!Number.isFinite(amount)) return '$—';
    if (amount === 0) return '$0.0000';
    if (amount < 0.0001) return `$${amount.toFixed(6)}`;
    if (amount < 1) return `$${amount.toFixed(4)}`;
    return `$${amount.toFixed(2)}`;
  }

  function usageCostLabel(item) {
    if (!item || Number(item.unpriced_requests || 0) >= Number(item.requests || 0)) {
      return item?.requests ? '费用未知' : formatUsageCost(0);
    }
    const incomplete = Number(item.unpriced_requests || 0) + Number(item.mixed_requests || 0);
    const suffix = incomplete > 0 ? ' + ?' : '';
    return `${formatUsageCost(item.cost)}${suffix}`;
  }

  function usageTotalCostLabel(item) {
    if (!item) return formatUsageCost(0);
    const base = Number(item.total_cost_with_background ?? item.cost ?? 0);
    const unknown = Number(item.unpriced_requests || 0)
      + Number(item.mixed_requests || 0)
      + Number(item.background_unpriced_operations || 0);
    if (unknown > 0) return `${formatUsageCost(base)} + ?`;
    return formatUsageCost(base);
  }

  function usageSourceLabel(source) {
    return ({
      upstream_exact: '上游实扣',
      local_estimate: '本机估算',
      legacy_recorded: '旧版记录',
      mixed: '混合计价',
      unavailable: '费用未知',
    })[source] || '费用未知';
  }

  function formatUsageTime(value) {
    if (!value) return '时间未知';
    const normalized = String(value).includes('T')
      ? String(value)
      : `${String(value).replace(' ', 'T')}Z`;
    const date = new Date(normalized);
    return Number.isNaN(date.getTime())
      ? String(value)
      : date.toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  }

  function renderApiUsage(data) {
    const summary = data.summary || {};
    const all = data.all_time || summary;
    const claudeCacheSummary = data.cache_summary || {};
    const claudeCacheAll = data.cache_all_time || claudeCacheSummary;
    const deepseekCacheSummary = data.deepseek_cache_summary || {};
    const deepseekCacheAll = data.deepseek_cache_all_time || deepseekCacheSummary;
    const setText = (selector, value) => {
      const el = $(selector);
      if (el) el.textContent = value;
    };

    const cacheProviderView = (claudeStats, deepseekStats) => {
      const claudeRequests = Number(claudeStats?.requests || 0);
      const deepseekRequests = Number(deepseekStats?.requests || 0);
      const claudeRead = Number(claudeStats?.cache_read || 0);
      const deepseekRead = Number(deepseekStats?.cache_read || 0);
      const claudePrompt = Number(claudeStats?.prompt_input_tokens || 0);
      const deepseekPrompt = Number(deepseekStats?.prompt_input_tokens || 0);
      if (deepseekRequests && !claudeRequests) {
        return {
          provider: 'deepseek',
          title: 'DeepSeek 缓存命中率',
          rate: Number(deepseekStats.cache_total_reuse_rate || 0),
          read: deepseekRead,
          secondary: Number(deepseekStats.cache_creation || 0),
          requestRate: Number(deepseekStats.cache_request_hit_rate || 0),
          saved: Number(deepseekStats.cache_saved || 0),
        };
      }
      if (claudeRequests && !deepseekRequests) {
        const creation = Number(claudeStats.cache_creation || 0);
        const fresh = Math.max(0, claudePrompt - claudeRead - creation);
        return {
          provider: 'claude',
          title: 'Claude 缓存命中率',
          // 人话口径：真正有多少输入 token 是从缓存直接复用的。
          // Claude Console 的 read/(read+fresh) 仍保留为下面的专业辅值。
          rate: Number(claudeStats.cache_total_reuse_rate || 0),
          read: claudeRead,
          secondary: creation,
          fresh,
          requestRate: Number(claudeStats.cache_request_hit_rate || 0),
          consoleRate: Number(claudeStats.cache_read_ratio || claudeStats.cache_token_hit_rate || 0),
          saved: Number(claudeStats.cache_saved || 0),
        };
      }
      if (claudeRequests || deepseekRequests) {
        const prompt = claudePrompt + deepseekPrompt;
        return {
          provider: 'mixed',
          title: '缓存总复用率',
          rate: prompt > 0 ? (claudeRead + deepseekRead) / prompt * 100 : 0,
          read: claudeRead + deepseekRead,
          secondary: Number(claudeStats?.cache_creation || 0) + Number(deepseekStats?.cache_creation || 0),
          claudeRate: Number(claudeStats?.cache_total_reuse_rate || 0),
          claudeConsoleRate: Number(claudeStats?.cache_read_ratio || 0),
          deepseekRate: Number(deepseekStats?.cache_total_reuse_rate || 0),
          saved: Number(claudeStats?.cache_saved || 0) + Number(deepseekStats?.cache_saved || 0),
        };
      }
      return { provider: 'none', title: '缓存命中率', rate: 0, read: 0, secondary: 0, saved: 0 };
    };

    const periodCache = cacheProviderView(claudeCacheSummary, deepseekCacheSummary);
    const allCache = cacheProviderView(claudeCacheAll, deepseekCacheAll);

    setText('#stat-tokens', formatNumber(Number(all.total_tokens || 0)));
    setText('#stat-cost', usageTotalCostLabel(all));
    setText('#stat-saved', formatUsageCost(allCache.saved));
    setText('#stat-cache-label', allCache.title);
    setText('#stat-cache', `${allCache.rate.toFixed(1)}%`);

    const callAudit = data.call_audit || {};
    const auditedUpstreamRequests = Number(callAudit.upstream_requests || 0);
    const auditedOperations = Number(callAudit.operations || 0);
    setText('#usage-period-cost', usageTotalCostLabel(summary));
    setText('#usage-period-requests', formatNumber(auditedUpstreamRequests || Number(summary.requests || 0)));
    setText('#usage-period-tokens', formatNumber(Number(summary.total_tokens || 0)));
    setText('#usage-cache-title', periodCache.title);
    setText('#usage-period-cache-rate', `${periodCache.rate.toFixed(1)}%`);
    setText('#usage-input', formatNumber(Number(summary.input_tokens || 0)));
    setText('#usage-output', formatNumber(Number(summary.output_tokens || 0)));
    setText('#usage-reasoning', formatNumber(Number(summary.reasoning_tokens || 0)));
    setText('#usage-cache-read', formatNumber(periodCache.read));
    setText('#usage-cache-write', formatNumber(periodCache.secondary));
    setText('#usage-cache-read-label', '缓存命中');
    setText('#usage-cache-write-label', periodCache.provider === 'deepseek' ? '缓存未命中' : (periodCache.provider === 'mixed' ? '新写 / 未命中' : '缓存新写'));
    setText('#usage-unpriced', auditedOperations
      ? `模型操作 ${formatNumber(auditedOperations)} 次 · 上游 ${formatNumber(auditedUpstreamRequests)} 次`
      : (summary.unpriced_requests ? `${summary.unpriced_requests} 笔费用未知` : '等待本版本产生真实调用审计'));
    if (periodCache.provider === 'deepseek') {
      setText('#usage-cache-saved', `命中 ${formatNumber(periodCache.read)} tokens · 未命中 ${formatNumber(periodCache.secondary)} · 请求命中 ${periodCache.requestRate.toFixed(1)}% · 节省 ${formatUsageCost(periodCache.saved)}`);
    } else if (periodCache.provider === 'claude') {
      setText('#usage-cache-saved', `命中 ${formatNumber(periodCache.read)} tokens · 新写 ${formatNumber(periodCache.secondary)} · 未缓存 ${formatNumber(periodCache.fresh || 0)} · 请求命中 ${periodCache.requestRate.toFixed(1)}% · Console 读取率 ${periodCache.consoleRate.toFixed(1)}% · 节省 ${formatUsageCost(periodCache.saved)}`);
    } else if (periodCache.provider === 'mixed') {
      setText('#usage-cache-saved', `DeepSeek 命中 ${periodCache.deepseekRate.toFixed(1)}% · Claude 命中 ${periodCache.claudeRate.toFixed(1)}% · 合计复用 ${formatNumber(periodCache.read)} tokens`);
    } else {
      setText('#usage-cache-saved', '本期还没有可统计的缓存请求');
    }

    const quality = [];
    if (summary.exact_requests) quality.push(`${summary.exact_requests} 笔上游实扣`);
    if (summary.estimated_requests) quality.push(`${summary.estimated_requests} 笔本机估算`);
    if (summary.legacy_requests) quality.push(`${summary.legacy_requests} 笔旧版记录`);
    if (summary.mixed_requests) quality.push(`${summary.mixed_requests} 笔混合计价`);
    if (summary.unpriced_requests) quality.push(`${summary.unpriced_requests} 笔费用未知`);
    if (summary.background_operations) {
      const bg = `后台 ${summary.background_operations} 次 / ${formatUsageCost(summary.background_cost)}`;
      quality.push(summary.background_unpriced_operations ? `${bg} + ?` : bg);
    }
    setText('#usage-cost-quality', quality.join(' · ') || '本期还没有 API 请求');

    const status = $('#api-usage-status');
    if (status) {
      status.classList.remove('error');
      const cacheExplanation = periodCache.provider === 'deepseek'
        ? 'DeepSeek 大数字直接显示真正的输入 token 缓存命中率：hit ÷ (hit + miss)；下方“未命中”就是上游返回的 prompt_cache_miss_tokens。'
        : periodCache.provider === 'claude'
          ? 'Claude 大数字直接显示真正的输入 Token 缓存命中率：cache_read ÷ 全部 prompt tokens；Console 读取率只作为下面的专业辅助指标。'
          : '同时使用多个模型时，大数字显示所有可识别缓存输入的总复用率，并在下方拆分 DeepSeek 与 Claude。';
      status.textContent = summary.unpriced_requests
        ? `注意：旧版 OpenRouter 的 0 元记录没有真实扣费依据，现已按“费用未知”处理。${cacheExplanation}`
        : `金额来源已标注；缓存节省按请求发生时保存的价格快照计算。${cacheExplanation}`;
    }

    const models = $('#api-usage-models');
    if (models) {
      models.innerHTML = (data.by_model || []).length
        ? data.by_model.map((item) => `
          <div class="api-usage-row">
            <span><strong>${esc(item.model || 'unknown')}</strong><small>${esc(item.provider || 'unknown')} · ${formatNumber(Number(item.requests || 0))} 次 · ${formatNumber(Number(item.total_tokens || 0))} tokens · ${String(item.provider || '').toLowerCase() === 'deepseek' ? `缓存命中 ${Number(item.cache_total_reuse_rate || 0).toFixed(1)}% · 命中 ${formatNumber(Number(item.cache_read || 0))} / 未命中 ${formatNumber(Number(item.cache_creation || 0))}` : `缓存命中 ${Number(item.cache_total_reuse_rate || 0).toFixed(1)}% · 命中 ${formatNumber(Number(item.cache_read || 0))} / 新写 ${formatNumber(Number(item.cache_creation || 0))} · Console ${Number(item.cache_read_ratio || item.cache_token_hit_rate || 0).toFixed(1)}%`}</small></span>
            <b>${usageCostLabel(item)}</b>
          </div>`).join('')
        : '<div class="api-usage-empty">这个时间范围还没有 API 请求</div>';
    }

    const recent = $('#api-usage-recent');
    if (recent) {
      recent.innerHTML = (data.recent || []).length
        ? data.recent.map((item) => `
          <div class="api-usage-row">
            <span><strong>${esc(item.model || 'unknown')}</strong><small>${formatUsageTime(item.created_at)} · ${formatNumber(Number(item.total_tokens || 0))} tokens · ${String(item.provider || '').toLowerCase() === 'deepseek' ? `缓存命中 ${Number(item.cache_total_reuse_rate || 0).toFixed(1)}% · 命中 ${formatNumber(Number(item.cache_read || 0))} / 未命中 ${formatNumber(Number(item.cache_creation || 0))}` : `缓存命中 ${Number(item.cache_total_reuse_rate || 0).toFixed(1)}% · 命中 ${formatNumber(Number(item.cache_read || 0))} / 新写 ${formatNumber(Number(item.cache_creation || 0))} · Console ${Number(item.cache_read_ratio || item.cache_token_hit_rate || 0).toFixed(1)}%`} · ${usageSourceLabel(item.cost_source)}</small></span>
            <b>${item.cost_source === 'unavailable' ? '未知' : `${formatUsageCost(item.cost)}${item.cost_source === 'mixed' ? ' + ?' : ''}`}</b>
          </div>`).join('')
        : '<div class="api-usage-empty">这个时间范围还没有最近请求</div>';
    }

    const purposeLabels = {
      main_chat: '主回复',
      tool_loop: '工具循环',
      relational_rewrite: '关系修正重写',
      proactive: '主动续话（旧记录）',
      proactive_decision: '主动续话判断 · 当前模型一次性',
      internal_chat: '内部推理（不写会话）',
      memory_merge: '记忆合并 · DeepSeek Flash',
      memory_chunk: '记忆切分 · DeepSeek Flash',
      memory_tag: '记忆标签 · DeepSeek Flash',
      memory_dehydrate: '记忆压缩 · DeepSeek Flash',
      memory_contradiction: '记忆矛盾确认 · DeepSeek Flash',
      memory_rerank: '记忆重排 · DeepSeek Flash',
      voice_translation: '语音逐行翻译 · DeepSeek Flash',
      unspecified: '未分类调用',
    };
    const purposes = $('#api-usage-purposes');
    if (purposes) {
      purposes.innerHTML = (data.by_purpose || []).length
        ? data.by_purpose.map((item) => `
          <div class="api-usage-row">
            <span><strong>${esc(purposeLabels[item.purpose] || item.purpose || '未分类调用')}</strong><small>${esc(item.provider || 'unknown')} · ${esc(item.model || 'unknown')} · 模型操作 ${formatNumber(Number(item.operations || 0))} 次 · 上游请求 ${formatNumber(Number(item.upstream_requests || 0))} 次 · 读 ${formatNumber(Number(item.cache_read || 0))} / 写 ${formatNumber(Number(item.cache_creation || 0))}</small></span>
            <b>${Number(item.unpriced_operations || 0) >= Number(item.operations || 0) && Number(item.operations || 0) ? '未知' : formatUsageCost(item.cost)}</b>
          </div>`).join('')
        : '<div class="api-usage-empty">真实调用审计从这个版本开始累计；旧记录不会伪造用途。</div>';
    }

    const proactiveTriggerLabels = {
      conversation_afterglow: '回合余韵',
      held_back_clear: '清空 / 未说出口',
      held_back_pause: '停顿 / 未说出口',
      held_back_orphan: '草稿遗留',
      morning_response: '晨间事件',
      morning_response_test: '晨间测试',
      independent_initiative: '独立主动',
      manual_initiative_test: '主动测试',
      unknown: '未分类',
    };
    const proactiveStats = $('#api-usage-proactive');
    if (proactiveStats) {
      proactiveStats.innerHTML = (data.proactive_by_trigger || []).length
        ? data.proactive_by_trigger.map((item) => `
          <div class="api-usage-row">
            <span><strong>${esc(proactiveTriggerLabels[item.trigger_kind] || item.trigger_kind || '未分类')}</strong><small>模型调用 ${formatNumber(Number(item.calls || 0))} 次 · wait ${formatNumber(Number(item.waits || 0))} · speak ${formatNumber(Number(item.speaks || 0))} · recheck ${formatNumber(Number(item.rechecks || 0))} · wait率 ${Number(item.wait_rate || 0).toFixed(1)}%</small></span>
            <b>${Number(item.unpriced_calls || 0) >= Number(item.calls || 0) && Number(item.calls || 0) ? '费用未知' : formatUsageCost(item.cost)}</b>
          </div>`).join('')
        : '<div class="api-usage-empty">8.2 起开始按触发类型记录主动模型调用、wait/speak 与费用。</div>';
    }

    const cacheDiagnostics = $('#api-cache-diagnostics');
    if (cacheDiagnostics) {
      const rows = data.cache_diagnostics || [];
      const warmers = data.cache_keepalive || [];
      const warmerHtml = warmers.map((item) => {
        const status = item.paused ? `已暂停 · ${esc(item.pause_reason || '保护触发')}` : (item.healthy ? '保温健康' : '等待真实命中确认');
        const next = Number(item.next_warm_at || 0) > 0 ? new Date(Number(item.next_warm_at) * 1000).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'}) : '—';
        return `<div class="api-usage-row"><span><strong>缓存保温 · ${status}</strong><small>${esc(item.model || 'Claude')} · 指纹 ${esc(String(item.fingerprint || '').slice(0,12) || '—')} · 下次约 ${esc(next)} · 已保温 ${formatNumber(Number(item.warm_count_total || 0))} 次</small></span><b>${item.paused ? 'STOP' : 'WARM'}</b></div>`;
      }).join('');
      const diagHtml = rows.length
        ? rows.map((item) => {
          const eventLabel = item.cache_event === 'read' ? '命中' : (item.cache_event === 'write' ? '写入' : '未缓存');
          const route = item.actual_provider
            ? `${esc(item.actual_provider)}${item.router_region ? ` · ${esc(item.router_region)}` : ''}`
            : (item.cache_event === 'read' ? '缓存回放（OR 不返回旧路由元数据）' : '路由元数据未返回');
          const prefix = String(item.cache_prefix_hash || '').slice(0, 12) || '—';
          const parent = String(item.cache_parent_hash || '').slice(0, 12) || '—';
          const shape = String(item.cache_shape_hash || '').slice(0, 12) || '—';
          const generation = String(item.generation_id || '').slice(0, 18) || '—';
          const split = `1h 写 ${formatNumber(Number(item.cache_creation_1h || 0))} / 5m 写 ${formatNumber(Number(item.cache_creation_5m || 0))}`;
          const diagnosisLabels = {
            cache_hit: '缓存命中',
            request_shape_changed: 'system/tools/reasoning/model 形状变化',
            history_prefix_broke: '历史父前缀不连续',
            model_changed: '实际模型变化',
            provider_changed: 'provider endpoint 变化',
            cold_write_or_expired: '冷写 / TTL 到期 / 路由漂移待排查',
            no_cache_tokens: '没有返回缓存 token；检查最小长度',
          };
          const diagnosis = diagnosisLabels[item.cache_diagnosis] || item.cache_diagnosis || '—';
          const rawBreakpoint = String(item.cache_breakpoint || '');
          const overlapOk = rawBreakpoint.includes('overlap=ok');
          const firstRequest = rawBreakpoint.includes('overlap=first_request');
          const changedMatch = rawBreakpoint.match(/first_diff=message\[(\d+)\]/);
          const continuity = firstRequest
            ? '首轮冷缓存'
            : overlapOk
              ? '历史连续 ✓'
              : changedMatch
                ? `历史断链 ⚠️ · 第 ${Number(changedMatch[1]) + 1} 条消息发生变化`
                : '历史连续性异常 ⚠️';
          const guardHuman = String(item.cache_guard_status || 'active') === 'active'
            ? '保护正常'
            : `保护：${String(item.cache_guard_status || '')}`;
          return `
            <div class="api-usage-row">
              <span><strong>${eventLabel} · ${esc(item.cache_ttl || '—')} · ${esc(continuity)} · ${esc(diagnosis)}</strong><small>${formatUsageTime(item.created_at)} · ${route} · 命中 ${formatNumber(Number(item.cache_read || 0))} / 新写 ${formatNumber(Number(item.cache_creation || 0))} · ${split}<br>${esc(guardHuman)} · 缓存前缀约 ${formatNumber(Number(item.cache_prefix_tokens_estimate || 0))} tokens（估） · 技术指纹 prefix ${esc(prefix)} / parent ${esc(parent)} / shape ${esc(shape)} · gen ${esc(generation)}</small></span>
              <b>${item.cache_event === 'read' ? '命中' : (item.cache_event === 'write' ? '新写' : '未命中')}</b>
            </div>`;
        }).join('')
        : '<div class="api-usage-empty">还没有新版 Claude 缓存诊断记录。发两轮相同前缀的消息后，这里会显示 read/write、1h 分档和前缀指纹。</div>';
      cacheDiagnostics.innerHTML = warmerHtml + diagHtml;
    }
  }

  async function loadApiUsageStats(period = state.apiUsagePeriod) {
    const status = $('#api-usage-status');
    if (status) {
      status.classList.remove('error');
      status.textContent = '正在读取本机统计…';
    }
    try {
      const data = await fetchJSON(`/api/console/usage?period=${encodeURIComponent(period)}`, {
        cache: 'no-store',
      });
      state.apiUsagePeriod = data.period || period;
      $('#api-usage-range')?.querySelectorAll('[data-usage-period]').forEach((button) => {
        button.classList.toggle('active', button.dataset.usagePeriod === state.apiUsagePeriod);
      });
      renderApiUsage(data);
    } catch (error) {
      if (status) {
        status.classList.add('error');
        status.textContent = `API 用量读取失败：${error.message}`;
      }
    }
  }

  async function loadConsoleData() {
    // 用量独立加载；别的面板失败时，金额和 token 仍然会显示。
    const usageTask = loadApiUsageStats();
    const [providersResult, sessionsResult] = await Promise.allSettled([
      fetchJSON('/api/providers', { cache: 'no-store' }),
      fetchJSON('/api/sessions', { cache: 'no-store' }),
    ]);

    if (providersResult.status === 'fulfilled') {
      const providers = providersResult.value || {};
      state.providers = providers;
      const serverActive = Object.keys(providers).find((name) => providers[name]?.active);
      const activeReady = Boolean(serverActive && providers[serverActive]?.api_key_configured);
      const selectedProvider = activeReady
        ? serverActive
        : (Object.keys(providers).find((name) => providers[name]?.api_key_configured) || serverActive);
      if (selectedProvider) {
        const selectedConf = providers[selectedProvider] || {};
        state.activeProvider = selectedProvider;
        state.activeModel = selectedConf.selected_model || selectedConf.default_model || state.activeModel;
      }
      updateModelPill();
      renderProviders(providers);
      await loadKeyManagerStatus();
    } else {
      console.warn('Provider 控制台读取失败:', providersResult.reason);
    }

    if (sessionsResult.status === 'fulfilled') {
      renderSessionCosts(sessionsResult.value || []);
    } else {
      console.warn('Session 费用读取失败:', sessionsResult.reason);
    }

    await Promise.allSettled([
      usageTask,
      loadDiagnosticsData(), loadStickerLibrary(), loadCoreData(),
      loadCharacterData(), loadLivingData(), loadCoPresenceData(),
      loadVoiceSettings(), loadOceanListenStatus(), loadRelationalHonestyData(), loadLocalDataStatus(),
    ]);
  }

  async function loadKeyManagerStatus() {
    if (!dom.keyCredential) return;
    try {
      const data = await fetchJSON('/api/credentials', { cache: 'no-store' });
      state.keyCredentials = data.credentials || {};
      renderKeyManagerStatus();
    } catch (error) {
      if (dom.keyManagerBadge) dom.keyManagerBadge.textContent = '读取失败';
      if (dom.keyManagerStatus) dom.keyManagerStatus.textContent = `Key 状态读取失败：${error.message}`;
    }
  }

  function renderKeyManagerStatus() {
    if (!dom.keyCredential) return;
    const name = dom.keyCredential.value;
    const status = state.keyCredentials[name] || {};
    const configured = Boolean(status.configured);
    const configuredCount = Object.values(state.keyCredentials).filter((item) => item?.configured).length;
    const verifiedCount = Object.values(state.keyCredentials).filter((item) => item?.validation === 'valid').length;
    if (dom.keyManagerBadge) {
      dom.keyManagerBadge.textContent = configuredCount
        ? `已保存 ${configuredCount}${verifiedCount ? ` · 已验证 ${verifiedCount}` : ''}`
        : '全部未配置';
      dom.keyManagerBadge.classList.toggle('ready', verifiedCount > 0);
    }
    if (dom.keyManagerStatus) {
      if (status.load_error) {
        dom.keyManagerStatus.textContent = `${status.label || name}：Key 文件读取失败：${status.load_error_detail || '请检查文件格式或权限'}`;
      } else if (!configured) {
        dom.keyManagerStatus.textContent = `${status.label || name}：未配置。只有点击“保存 Key”后才会写入当前 jtyhome。`;
      } else if (status.validation === 'valid') {
        dom.keyManagerStatus.textContent = `${status.label || name}：已保存并通过远端验证。页面不会回显明文。`;
      } else if (status.validation === 'unverified') {
        dom.keyManagerStatus.textContent = `${status.label || name}：已保存，但暂未能远端验证（${status.validation_detail || '网络不可用'}）。`;
      } else {
        dom.keyManagerStatus.textContent = `${status.label || name}：已保存，尚未验证。`;
      }
    }
    if (dom.btnKeyClear) dom.btnKeyClear.disabled = !configured;
  }

  function toggleKeyVisibility() {
    if (!dom.keyValue) return;
    const show = dom.keyValue.type === 'password';
    dom.keyValue.type = show ? 'text' : 'password';
    if (dom.btnKeyToggle) dom.btnKeyToggle.textContent = show ? '隐藏' : '显示';
  }

  async function saveKeyFromUI() {
    if (!dom.keyCredential || !dom.keyValue) return;
    const credential = dom.keyCredential.value;
    const value = dom.keyValue.value.trim();
    if (!value) {
      if (dom.keyManagerStatus) dom.keyManagerStatus.textContent = '请先输入 Key；空白内容不会保存。';
      dom.keyValue.focus();
      return;
    }
    if (dom.btnKeySave) dom.btnKeySave.disabled = true;
    try {
      const saveResult = await fetchJSON('/api/credentials/save', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ credential, value }),
      });
      // Never retain the secret in the DOM after the explicit save completes.
      dom.keyValue.value = '';
      dom.keyValue.type = 'password';
      if (dom.btnKeyToggle) dom.btnKeyToggle.textContent = '显示';
      await loadProviders(false);
      await loadKeyManagerStatus();
      await loadModelsForProvider(state.activeProvider, true);
      refreshHealth();
      if (dom.keyManagerStatus) {
        const label = state.keyCredentials[credential]?.label || credential;
        dom.keyManagerStatus.textContent = saveResult.validation === 'valid'
          ? `${label}：保存成功并通过远端验证，当前输入框已清空。`
          : `${label}：已保存；${saveResult.validation_detail || '暂无法完成远端验证'}。输入框已清空。`;
      }
    } catch (error) {
      if (dom.keyManagerStatus) dom.keyManagerStatus.textContent = `保存失败：${error.message}`;
    } finally {
      if (dom.btnKeySave) dom.btnKeySave.disabled = false;
    }
  }

  async function clearKeyFromUI() {
    if (!dom.keyCredential) return;
    const credential = dom.keyCredential.value;
    const status = state.keyCredentials[credential] || {};
    if (!status.configured) return;
    const label = status.label || credential;
    if (!window.confirm(`确定清除 ${label}？这只会删除当前 jtyhome 保存的这一项 Key。`)) return;
    if (dom.btnKeyClear) dom.btnKeyClear.disabled = true;
    try {
      await fetchJSON('/api/credentials/clear', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ credential }),
      });
      if (dom.keyValue) dom.keyValue.value = '';
      await loadProviders(false);
      await loadKeyManagerStatus();
      refreshHealth();
      if (dom.keyManagerStatus) dom.keyManagerStatus.textContent = `${label}：已清除。`;
    } catch (error) {
      if (dom.keyManagerStatus) dom.keyManagerStatus.textContent = `清除失败：${error.message}`;
    } finally {
      if (dom.btnKeyClear) {
        dom.btnKeyClear.disabled = !Boolean(state.keyCredentials[credential]?.configured);
      }
    }
  }

  function renderProviders(providers) {
    const grid = $('#provider-switcher');
    if (!grid) return;
    grid.innerHTML = Object.entries(providers).map(([name, conf]) => {
      const active = name === state.activeProvider;
      const ready = conf.api_key_configured;
      const isPMode = name === 'claude_code_p';
      const validation = conf.credential_validation || 'unknown';
      const verified = ready && (validation === 'valid' || (isPMode && validation === 'available'));
      const sourceLabel = conf.api_key_source_label || '';
      const credentialLabel = isPMode
        ? (ready ? (active ? '使用中' : '订阅CLI') : '缺少CLI')
        : (!ready
          ? '缺少Key'
          : verified
            ? (active ? '使用中' : '已验证')
            : '待验证');
      const credentialTitle = isPMode
        ? (ready
            ? `${sourceLabel || 'Claude Code CLI 已安装'}；认证走 claude auth login 订阅 OAuth`
            : '未找到 Claude Code CLI。请先安装 Claude Code 并运行 claude auth login')
        : (conf.credential_load_error
          ? (conf.credential_load_error_detail || `${conf.credential_env || 'API Key'} 读取失败，请检查 Key 文件格式`)
          : ready
            ? (verified
                ? `${conf.credential_env || 'API Key'} 已通过远端验证 · 来自${sourceLabel || '当前配置'}`
                : `${conf.credential_env || 'API Key'} 已保存但尚未验证${conf.credential_validation_detail ? `：${conf.credential_validation_detail}` : ''}`)
            : `${conf.credential_env || 'API Key'} 未配置${sourceLabel ? `：${sourceLabel}` : ''}`);
      return `
        <button class="provider-btn ${active ? 'active' : ''}" data-provider="${esc(name)}" title="${esc(credentialTitle)}">
          <div>
            <div class="provider-name">${esc(conf.display_name || name)}</div>
            <div class="provider-model">${esc(conf.selected_model || conf.default_model || '')}</div>
          </div>
          <span class="provider-badge ${verified ? 'ready' : (ready ? 'pending' : 'missing')}">${esc(credentialLabel)}</span>
        </button>`;
    }).join('');

    grid.querySelectorAll('.provider-btn').forEach((btn) => {
      btn.addEventListener('click', () => selectProvider(btn.dataset.provider));
    });
  }

  async function selectProvider(provider) {
    const conf = state.providers[provider];
    if (!conf) return;
    if (!conf.api_key_configured) {
      if (provider === 'claude_code_p') {
        alert('没有找到 Claude Code CLI。请先安装 Claude Code，并在终端运行 claude auth login 登录订阅账号。');
      } else {
        alert(`${conf.credential_env || 'API Key'} 还没有填写`);
      }
      return;
    }
    const savedModel = savedModelForProvider(provider);
    const model = savedModel || conf.selected_model || conf.default_model;
    try {
      const resp = await fetch('/api/providers/switch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ provider, model }),
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.error || '切换失败');
      state.activeProvider = provider;
      state.activeModel = data.model || model;
      localStorage.setItem('companion:provider', provider);
      await loadProviders(false);
      await loadModelsForProvider(provider);
      updateModelPill();
      refreshHealth();
    } catch (e) {
      alert('切换失败: ' + e.message);
    }
  }

  async function loadModelsForProvider(provider, force = false) {
    if (!provider) return;
    if (dom.modelListMeta) dom.modelListMeta.textContent = '正在读取模型列表…';
    try {
      const resp = await fetch(
        `/api/providers/${encodeURIComponent(provider)}/models${force ? '?refresh=true' : ''}`,
        { cache: 'no-store', credentials: 'same-origin' },
      );
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.error || '模型列表读取失败');
      state.modelPayload = data;
      state.models = Array.isArray(data.models) ? data.models : [];
      state.activeBrainCapabilities = data.selected_brain_capabilities || null;
      if (!state.activeModel || provider !== state.activeProvider) {
        state.activeModel = data.selected_model || data.default_model || '';
      }
      if (provider === state.activeProvider && data.selected_model) {
        state.activeModel = savedModelForProvider(provider) || data.selected_model;
      }
      const activeItem = state.models.find((item) => item.id === state.activeModel);
      state.activeBrainCapabilities = activeItem?.brain_capabilities || data.selected_brain_capabilities || null;
      renderModelList();
      renderBrainSettings();
      updateModelPill();
      renderAttachmentTray();
    } catch (e) {
      state.models = [];
      if (dom.modelListMeta) dom.modelListMeta.textContent = `读取失败：${e.message}`;
      if (dom.modelList) dom.modelList.innerHTML = '';
    }
  }

  function renderModelList() {
    if (!dom.modelList) return;
    const query = (dom.modelSearch?.value || '').trim().toLowerCase();
    const filtered = state.models.filter((m) => {
      const hay = `${m.id || ''} ${m.label || ''}`.toLowerCase();
      return !query || hay.includes(query);
    });
    const shown = filtered.slice(0, 240);
    const source = state.modelPayload?.source === 'remote' ? '在线列表' : '内置收藏';
    const warning = state.modelPayload?.warning ? ` · 在线刷新失败，已回退` : '';
    dom.modelListMeta.textContent = `${source} · ${filtered.length} 个模型${warning}${filtered.length > shown.length ? ` · 显示前 ${shown.length} 个` : ''}`;
    dom.modelList.innerHTML = shown.map((m) => {
      const active = m.id === state.activeModel;
      const context = m.context_length ? `${formatNumber(m.context_length)} context` : '';
      return `
        <button class="model-item ${active ? 'active' : ''}" data-model="${esc(m.id)}">
          <span class="model-item-main">
            <strong>${esc(m.label || m.id)}</strong>
            <small>${esc(m.id)}</small>
          </span>
          <span class="model-item-side">${active ? '当前' : esc(context)}</span>
        </button>`;
    }).join('') || '<div class="model-empty">没有匹配的模型，可以在上面直接输入模型 ID。</div>';
    dom.modelList.querySelectorAll('.model-item').forEach((btn) => {
      btn.addEventListener('click', () => selectModel(btn.dataset.model));
    });
  }

  async function selectModel(model) {
    try {
      const resp = await fetch('/api/models/switch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ provider: state.activeProvider, model }),
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.error || '模型切换失败');
      state.activeModel = data.model;
      state.activeBrainCapabilities = data.brain_capabilities || null;
      localStorage.setItem(`companion:model:${state.activeProvider}`, data.model);
      if (dom.customModelId) dom.customModelId.value = '';
      if (state.providers[state.activeProvider]) {
        state.providers[state.activeProvider].selected_model = data.model;
      }
      renderProviders(state.providers);
      renderModelList();
      renderBrainSettings();
      updateModelPill();
      refreshHealth();
    } catch (e) {
      alert('模型切换失败: ' + e.message);
    }
  }

  function brainStorageKey(provider) {
    return `companion:brain:${provider}`;
  }

  function modelMatchesProvider(provider, model) {
    const value = String(model || '').replace(/^~/, '');
    if (!value) return true;
    if (provider === 'openrouter_claude') return value.startsWith('anthropic/');
    if (provider === 'openrouter_gpt') return value.startsWith('openai/');
    return true;
  }

  function savedModelForProvider(provider) {
    const key = `companion:model:${provider}`;
    let model = localStorage.getItem(key) || '';
    if (provider === 'deepseek' && ['deepseek-chat', 'deepseek-reasoner'].includes(model)) {
      model = 'deepseek-v4-flash';
      localStorage.setItem(key, model);
    }
    if (!modelMatchesProvider(provider, model)) {
      localStorage.removeItem(key);
      return '';
    }
    return model;
  }

  function loadBrainOptions(provider) {
    const conf = state.providers[provider] || {};
    const defaults = conf.default_options || state.modelPayload?.default_options || {};
    let saved = {};
    try {
      saved = JSON.parse(localStorage.getItem(brainStorageKey(provider)) || '{}');
    } catch (_) {}
    state.brainOptions[provider] = { ...defaults, ...saved, sticker_mode: state.stickerMode || saved.sticker_mode || 'off' };
    return state.brainOptions[provider];
  }

  function saveBrainOptionsFromUI() {
    const provider = state.activeProvider;
    if (!provider) return;
    const options = {
      reasoning_effort: dom.reasoningEffort?.value || 'auto',
      thinking_mode: dom.thinkingMode?.value || 'auto',
      reasoning_context: dom.reasoningContext?.value || 'auto',
      thinking_budget: Number(dom.thinkingBudget?.value || 8000),
      thinking_visibility: dom.thinkingVisibility?.value || 'full',
      verbosity: dom.verbosity?.value || 'auto',
      max_output_tokens: Number(dom.maxOutputTokens?.value || 32768),
      tool_mode: (state.brainOptions[provider] || {}).tool_mode || 'auto',
      sticker_mode: state.stickerMode || 'off',
    };
    state.brainOptions[provider] = options;
    localStorage.setItem(brainStorageKey(provider), JSON.stringify(options));
    renderBrainSettings();
  }

  function getBrainOptionsForRequest() {
    const options = state.brainOptions[state.activeProvider] || loadBrainOptions(state.activeProvider);
    return { ...options };
  }

  function setSelectOptions(select, items, selected) {
    if (!select) return;
    select.innerHTML = items.map(([value, label]) => `<option value="${esc(value)}">${esc(label)}</option>`).join('');
    select.value = items.some(([value]) => value === selected) ? selected : items[0][0];
  }

  function renderBrainSettings() {
    const provider = state.activeProvider;
    const conf = state.providers[provider] || {};
    const activeItem = state.models.find((item) => item.id === state.activeModel);
    const profile = activeItem?.brain_capabilities || state.activeBrainCapabilities || conf.brain_capabilities || {};
    const providerCaps = conf.capabilities || state.modelPayload?.capabilities || {};
    const options = state.brainOptions[provider] || loadBrainOptions(provider);
    const protocolName = profile.api_family || providerCaps.native_api || conf.protocol || '兼容接口';
    const isClaudeProtocol = protocolName === 'messages' || protocolName === 'claude_code_stream_json' || conf.protocol === 'anthropic' || conf.protocol === 'claude_code_p';
    const isGPTProtocol = protocolName === 'responses' || conf.protocol === 'openai_responses';
    if (dom.brainProtocol) dom.brainProtocol.textContent = `${conf.display_name || provider || '未选择服务商'} · ${protocolName}`;

    const badges = [];
    if (profile.reasoning_control) badges.push('模型级可控思考');
    if (profile.supports_images) badges.push('原生图片');
    if (profile.supports_pdf) badges.push('原生文件');
    if (profile.supports_local_files) badges.push('本机文件解析');
    if (profile.supports_tools) badges.push('原生工具');
    else if ((providerCaps.tools_mode || '') === 'diagnostic_prefetch') badges.push('诊断快照');
    badges.push(profile.source ? `能力表：${profile.source}` : '保守能力表');
    if (dom.brainCapabilityBadges) {
      dom.brainCapabilityBadges.innerHTML = badges.map((x, i) => `<span class="brain-badge ${i === badges.length - 1 ? 'muted' : ''}">${esc(x)}</span>`).join('');
    }

    const efforts = Array.isArray(profile.reasoning_efforts) ? profile.reasoning_efforts : [];
    const modes = Array.isArray(profile.reasoning_modes) ? profile.reasoning_modes : [];
    const contexts = Array.isArray(profile.reasoning_contexts) ? profile.reasoning_contexts : [];
    const visibleThinkingProviders = new Set([
      'deepseek', 'anthropic', 'openai',
      'openrouter', 'openrouter_claude', 'openrouter_gpt', 'claude_code_p',
    ]);
    dom.reasoningEffortWrap?.classList.toggle('hidden', efforts.length === 0);
    dom.thinkingModeWrap?.classList.toggle('hidden', modes.length === 0);
    dom.thinkingVisibilityWrap?.classList.toggle(
      'hidden',
      !profile.reasoning_control && !visibleThinkingProviders.has(provider),
    );
    dom.reasoningContextWrap?.classList.toggle('hidden', contexts.length === 0);
    dom.verbosityWrap?.classList.toggle('hidden', !profile.supports_verbosity);

    const effortLabels = {
      auto: '自动（模型默认）', none: '无额外推理', minimal: '极快', low: '快速',
      medium: '标准', high: '深入', xhigh: '极深', max: '最大',
    };
    if (efforts.length) {
      setSelectOptions(dom.reasoningEffort, efforts.map((value) => [value, effortLabels[value] || value]), String(options.reasoning_effort || profile.default_reasoning_effort || efforts[0]));
    }

    const modeLabels = {
      auto: '自动（推荐）', off: '关闭', adaptive: '自适应', manual: '固定预算',
      on: '开启', enabled: '开启思考', disabled: '关闭思考', pro: 'Pro 满功率',
    };
    if (modes.length) {
      setSelectOptions(dom.thinkingMode, modes.map((value) => [value, modeLabels[value] || value]), String(options.thinking_mode || profile.default_reasoning_mode || modes[0]));
    }
    if (dom.thinkingModeLabel) {
      dom.thinkingModeLabel.textContent = isClaudeProtocol ? 'Claude 思考模式' : (provider === 'deepseek' ? 'DeepSeek 思考模式' : 'GPT 推理模式');
    }
    const contextLabels = { auto: '自动', current_turn: '仅当前轮', all_turns: '跨轮连续' };
    if (contexts.length) {
      setSelectOptions(dom.reasoningContext, contexts.map((value) => [value, contextLabels[value] || value]), String(options.reasoning_context || 'auto'));
    }
    const manual = isClaudeProtocol && dom.thinkingMode?.value === 'manual';
    dom.thinkingBudgetWrap?.classList.toggle('hidden', !manual);
    if (dom.thinkingBudget) dom.thinkingBudget.value = Number(options.thinking_budget || 8000);
    if (dom.thinkingVisibility) dom.thinkingVisibility.value = String(options.thinking_visibility || 'full');
    if (dom.verbosity) dom.verbosity.value = String(options.verbosity || 'auto');
    if (dom.maxOutputTokens) {
      const fallback = 32768;
      dom.maxOutputTokens.max = Number(profile.max_output_tokens || 131072);
      dom.maxOutputTokens.value = Math.min(Number(options.max_output_tokens || fallback), Number(dom.maxOutputTokens.max));
    }

    if (dom.brainSettingsNote) {
      const notes = Array.isArray(profile.notes) ? profile.notes.join(' ') : '';
      if (provider === 'openrouter_gpt') {
        dom.brainSettingsNote.textContent = `GPT 使用官方 OpenAI SDK 连接 OpenRouter Responses API Beta。上游返回可读推理摘要时，会按“思考展示”设置展开并单独保存在本机；不支持的参数不会误发。${notes ? ` ${notes}` : ''}`;
      } else if (provider === 'openrouter_claude') {
        dom.brainSettingsNote.textContent = `Claude 使用官方 Anthropic SDK 连接 OpenRouter Messages 原生格式，并由 OpenRouter 自动选择可用节点。API 返回的可见思考摘要会按设置展开并单独保存在本机；原生文件与工具结构仍会保留。${notes ? ` ${notes}` : ''}`;
      } else if (provider === 'claude_code_p') {
        dom.brainSettingsNote.textContent = `P 模式通过本机 Claude Code -p + stream-json 常驻进程走订阅账号的 Agent SDK/claude -p 额度，不需要 API Key。主聊天按窗口隔离进程；浏览器断线不会主动杀进程。${notes ? ` ${notes}` : ''}`;
      } else if (isGPTProtocol) {
        dom.brainSettingsNote.textContent = `GPT 使用官方 OpenAI SDK 与 Responses API。API 返回可读推理摘要时可展开并单独保存在本机；不支持的参数不会发送。${notes ? ` ${notes}` : ''}`;
      } else if (isClaudeProtocol) {
        dom.brainSettingsNote.textContent = `Claude 使用官方 Anthropic SDK 与 Messages API。自适应和固定预算不会混用；API 明确返回的可见思考可按设置展开并单独保存在本机。${notes ? ` ${notes}` : ''}`;
      } else if (provider === 'deepseek') {
        dom.brainSettingsNote.textContent = `DeepSeek V4 使用独立 Chat Completions 能力卡。API 返回的 reasoning_content 可完整展开并单独保存在本机；它不会混入可见回答，也不会在切换 Claude / GPT 后发送过去。${notes ? ` ${notes}` : ''}`;
      } else {
        dom.brainSettingsNote.textContent = `该模型走独立兼容适配器，不会继承 GPT/Claude 的原生参数；若上游返回可见推理字段，系统会按“思考展示”设置处理。${notes ? ` ${notes}` : ''}`;
      }
    }
  }

  async function runBrainIntegrityPreview() {
    if (!dom.btnBrainPreview || !dom.brainPreviewOutput) return;
    dom.btnBrainPreview.disabled = true;
    dom.btnBrainPreview.textContent = '预检中…';
    try {
      const resp = await fetch('/api/brain/integrity/preview', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          provider: state.activeProvider,
          model: state.activeModel,
          options: getBrainOptionsForRequest(),
        }),
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.error || '预检失败');
      dom.brainPreviewOutput.textContent = formatIntegrity(data, true);
      const details = dom.brainPreviewOutput.closest('details');
      if (details) details.open = true;
    } catch (e) {
      dom.brainPreviewOutput.textContent = `预检失败：${e.message}`;
    } finally {
      dom.btnBrainPreview.disabled = false;
      dom.btnBrainPreview.textContent = '离线接线预检';
    }
  }

  function updateModelPill() {
    if (!dom.modelPillText) return;
    const conf = state.providers[state.activeProvider] || {};
    const providerName = conf.display_name || state.activeProvider || 'API';
    const shortModel = (state.activeModel || conf.default_model || '未选模型').replace(/^.*\//, '');
    dom.modelPillText.textContent = `${providerName} · ${shortModel}`;
    dom.modelPillText.title = `${providerName} / ${state.activeModel || ''}`;
  }

  async function refreshHealth() {
    if (!dom.healthDot) return;
    try {
      const resp = await fetch('/api/health', { cache: 'no-store' });
      const data = await resp.json();
      const provider = data.provider || {};
      const database = data.database || {};
      const memory = data.memory || {};
      const healthy = Boolean(data.ok && database.ok && memory.worker_running);
      const keyReady = Boolean(provider.api_key_configured);
      dom.healthDot.className = `health-dot ${healthy && keyReady ? 'good' : (healthy ? 'warn' : 'bad')}`;
      dom.btnModelHub.title = keyReady
        ? `${provider.name || ''} / ${provider.model || ''} · ${healthy ? '系统正常' : '系统有异常'}`
        : `${provider.name || '当前接口'} 尚未配置 API Key`;
    } catch (_) {
      dom.healthDot.className = 'health-dot bad';
      dom.btnModelHub.title = '后端连接失败';
    }
  }

  const CORE_ORDER = [
    'joy', 'intimacy', 'longing', 'jealousy', 'possessiveness', 'lust',
    'protectiveness', 'playfulness', 'curiosity', 'satisfaction',
    'energy', 'fatigue', 'anxiety', 'sadness', 'irritation', 'fear'
  ];

  function tenLevel(value) {
    return Math.max(1, Math.min(10, Math.round(Number(value || 0) * 9) + 1));
  }

  async function loadCoreData() {
    if (!dom.coreSummary) return;
    try {
      const [stateResp, pluginsResp, archiveResp, historyResp] = await Promise.all([
        fetch('/api/core/state', { cache: 'no-store' }),
        fetch('/api/core/plugins', { cache: 'no-store' }),
        fetch('/api/memory/archive?query=&limit=1', { cache: 'no-store' }),
        fetch('/api/core/events?limit=12', { cache: 'no-store' }),
      ]);
      const data = await stateResp.json();
      const pluginData = await pluginsResp.json();
      const archiveData = await archiveResp.json();
      const historyData = await historyResp.json();
      renderCoreState(data, pluginData.plugins || [], archiveData.stats || {}, historyData.intentions || []);
    } catch (e) {
      dom.coreSummary.textContent = `读取失败：${e.message}`;
    }
  }

  function renderCoreState(data, plugins, archive, intentions) {
    const labels = data.labels || {};
    const values = data.dimensions || {};
    dom.coreSummary.textContent = data.summary || '平静';
    dom.archiveCount.textContent = `${Number(archive.raw_messages || 0)} 条原话`;
    if (dom.coreIntimacyMode) dom.coreIntimacyMode.value = data.settings?.intimacy_mode || 'active';

    const intention = data.intention;
    if (intention) {
      dom.coreIntention.innerHTML = `
        <div>
          <span class="core-kicker">当前亲密意图 · ${esc(intention.status || 'pending')}</span>
          <strong>${esc(intention.text || '')}</strong>
          <small>${esc(intention.trigger_reason || '')}</small>
        </div>
        <button id="btn-release-intention" class="btn-soft" type="button">让它自己放下</button>`;
      $('#btn-release-intention')?.addEventListener('click', releaseCoreIntention);
    } else {
      const blocked = data.meta?.blocked_until_affection;
      dom.coreIntention.innerHTML = `<div><span class="core-kicker">亲密意图</span><strong>${blocked ? '已暂停，等待你重新主动亲近' : '目前没有待处理的念头'}</strong><small>明确拒绝会立即关闭本次推进；情绪余波仍可保留。</small></div>`;
    }

    const meta = data.meta || {};
    dom.coreMeta.innerHTML = `
      <span>情绪余波 ${Number(meta.frustration || 0).toFixed(2)} / 3</span>
      <span>连续拒绝 ${Number(meta.rejection_streak || 0)}</span>
      <span>互动轮次 ${Number(meta.turn_count || 0)}</span>
      <span>${meta.blocked_until_affection ? '亲密推进已暂停' : '亲密推进可用'}</span>`;

    dom.coreBars.innerHTML = CORE_ORDER.filter((key) => key in values).map((key) => `
      <div class="core-bar-row ${['jealousy','possessiveness','lust'].includes(key) ? 'core-hot' : ''}">
        <span>${esc(labels[key] || key)}</span>
        <div class="core-track"><i style="width:${tenLevel(values[key]) * 10}%"></i></div>
        <em>${tenLevel(values[key])}/10</em>
      </div>`).join('');

    const events = data.recent_events || [];
    dom.coreEvents.innerHTML = events.length ? events.map((item) => {
      const deltas = item.deltas || {};
      const changed = Object.entries(deltas).filter(([, v]) => Math.abs(Number(v)) >= 0.005).slice(0, 4)
        .map(([k, v]) => `${labels[k] || k} ${Number(v) > 0 ? '+' : ''}${Math.round(Number(v) * 100)}`).join(' · ');
      return `<div class="diagnostic-row static"><span>${esc(item.reason || item.event_type || '')}</span><small>${esc(changed || '状态已记录')}</small><em>${esc(item.created_at ? formatShortDate(item.created_at) : '')}</em></div>`;
    }).join('') : '<div class="diagnostic-empty">还没有情绪变化记录</div>';

    dom.corePlugins.innerHTML = plugins.length ? plugins.map((item) => `
      <div class="diagnostic-row static">
        <span>${item.health === 'ok' ? '✓' : '⚠'} ${esc(item.display_name || item.name)}</span>
        <small>${esc(item.detail || item.description || '')}</small>
        <em>${esc(item.source || '')}</em>
      </div>`).join('') : '<div class="diagnostic-empty">插件注册表还是空的</div>';

    const statusLabels = {
      pending: '待表达', expressed: '已表达，等待回应', satisfied: '已回应',
      soft_rejected: '婉拒后关闭', hard_rejected: '明确拒绝后关闭',
      self_released: '主动放下', expired: '自然过期', reset: '状态重置'
    };
    dom.coreIntentions.innerHTML = intentions.length ? intentions.map((item) => `
      <div class="diagnostic-row static">
        <span>${esc(statusLabels[item.status] || item.status || '')} · ${esc(item.text || '')}</span>
        <small>${esc(item.resolution_note || item.trigger_reason || '')}</small>
        <em>${esc(item.created_at ? formatShortDate(item.created_at) : '')}</em>
      </div>`).join('') : '<div class="diagnostic-empty">还没有亲密意图记录</div>';
  }

  async function searchRawArchive() {
    if (!dom.archiveResults) return;
    const query = (dom.archiveQuery?.value || '').trim();
    if (!query) {
      dom.archiveResults.innerHTML = '<div class="diagnostic-empty">先输入一个记得的词或句子</div>';
      return;
    }
    dom.archiveResults.innerHTML = '<div class="diagnostic-empty">正在翻原话…</div>';
    try {
      const resp = await fetch(`/api/memory/archive?query=${encodeURIComponent(query)}&limit=8`, { cache: 'no-store' });
      const data = await resp.json();
      const items = data.items || [];
      dom.archiveResults.innerHTML = items.length ? items.map((item) => `
        <button class="diagnostic-row archive-source-row" data-message-id="${Number(item.message_id || 0)}">
          <span>${item.conversation_deleted ? '来源摘录' : (item.role === 'assistant' ? esc(companionName) : '你')} · 消息 #${Number(item.message_id || 0)}</span>
          <small>${esc(item.quote || item.content || '')}</small>
          <em>${esc(item.created_at ? formatShortDate(item.created_at) : '')} · 匹配 ${Number(item.score || 0).toFixed(1)}${item.conversation_deleted ? ' · 原对话已删除' : ''}</em>
        </button>`).join('') : '<div class="diagnostic-empty">没有找到匹配原话</div>';
      dom.archiveResults.querySelectorAll('[data-message-id]').forEach((button) => {
        button.addEventListener('click', () => loadArchiveSource(button.dataset.messageId));
      });
    } catch (e) {
      dom.archiveResults.innerHTML = `<div class="diagnostic-empty">搜索失败：${esc(e.message)}</div>`;
    }
  }

  async function loadArchiveSource(messageId) {
    const resp = await fetch(`/api/memory/source/${encodeURIComponent(messageId)}`);
    const data = await resp.json();
    showDiagnosticDetail({
      kind: data.conversation_deleted ? '已删除对话的来源摘录' : '不可改写原文',
      message_id: data.message_id,
      session_id: data.session_id,
      role: data.role,
      created_at: data.created_at,
      content: data.content,
      content_hash: data.content_hash,
      note: data.conversation_deleted ? '完整对话已按你的操作删除；这里只保留了长期记忆创建时的精确摘录，用于核对来源。' : '',
    });
  }

  async function updateCoreSettings() {
    if (!dom.coreIntimacyMode) return;
    const resp = await fetch('/api/core/settings', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ intimacy_mode: dom.coreIntimacyMode.value }),
    });
    if (!resp.ok) alert('设置保存失败');
    await loadCoreData();
  }

  async function resetCoreState() {
    if (!confirm('重置即时情绪和心境吗？亲密模式设置会保留。')) return;
    await fetch('/api/core/reset', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ keep_settings: true }),
    });
    await loadCoreData();
  }

  async function releaseCoreIntention() {
    await fetch('/api/core/intention/release', { method: 'POST' });
    await loadCoreData();
  }

  // ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  //  v5.8.2 性格脊柱
  // ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  const boolValue = (element, fallback = true) => element ? element.value === 'true' : fallback;

  function renderPersonaLab(lab = {}) {
    const sampleCount = Number(lab.sample_count || 0);
    const score = lab.score == null ? null : Math.max(0, Math.min(100, Number(lab.score || 0)));
    if (dom.personaLabScore) {
      dom.personaLabScore.textContent = score == null ? '—' : String(Math.round(score));
      dom.personaLabScore.closest('.persona-score-ring')?.style.setProperty('--persona-score', String(score || 0));
    }
    const contracts = Array.isArray(lab.contracts) ? lab.contracts : [];
    if (dom.personaLabContract) {
      dom.personaLabContract.innerHTML = contracts.length ? contracts.map((item) => `
        <div class="persona-contract-item${item.ok ? '' : ' warn'}">
          <i aria-hidden="true"></i><span>${esc(item.label || item.key || '人格约束')}</span>
        </div>`).join('') : '<div class="persona-lab-empty">人格约束尚未载入。</div>';
    }
    const metrics = lab.metrics || {};
    if (dom.personaLabMetrics) {
      dom.personaLabMetrics.innerHTML = `
        <div><span>检查回复</span><strong>${sampleCount.toLocaleString()}</strong></div>
        <div><span>身份漂移</span><strong>${Number(metrics.identity_drift || 0).toLocaleString()}</strong></div>
        <div><span>称呼颠倒</span><strong>${Number(metrics.address_reversal || 0).toLocaleString()}</strong></div>
        <div><span>表达过热</span><strong>${(Number(metrics.watch_hits || 0) + Number(metrics.dash_over_limit || 0)).toLocaleString()}</strong></div>`;
    }
    const issues = Array.isArray(lab.issues) ? lab.issues : [];
    if (dom.personaLabIssues) {
      dom.personaLabIssues.innerHTML = issues.length ? issues.slice(0, 10).map((item) => `
        <div class="persona-issue-row severity-${esc(item.severity || 'low')}">
          <span>#${Number(item.message_id || 0)}</span>
          <strong>${esc(item.label || '需要留意')}</strong>
          <p title="${esc(item.excerpt || '')}">${esc(item.excerpt || '没有摘录')}</p>
        </div>`).join('') : `<div class="persona-lab-empty">${sampleCount ? '最近样本没有发现身份漂移、称呼颠倒或明显表达过热。' : '当前窗口还没有可检查的助手回复。'}</div>`;
    }
  }

  async function loadCharacterData() {
    if (!dom.characterNativeSummary) return;
    if (dom.btnCharacterLab) {
      dom.btnCharacterLab.disabled = true;
      dom.btnCharacterLab.textContent = '检查中…';
    }
    try {
      const query = state.currentSession ? `?session_id=${encodeURIComponent(state.currentSession)}` : '';
      const resp = await fetch(`/api/character/state${query}`, { cache: 'no-store' });
      const data = await resp.json();
      const settings = data.settings || {};
      const analysis = data.analysis || {};
      dom.characterEnabled.value = String(settings.enabled !== false);
      dom.phraseFatigue.value = String(settings.phrase_fatigue !== false);
      dom.punctuationFatigue.value = String(settings.punctuation_fatigue !== false);
      dom.watchPhrases.value = (settings.watch_phrases || []).join('，');
      dom.characterNativeSummary.textContent = settings.native_voice !== false ? 'Claude / GPT 原样保留' : '已关闭';
      const tired = [...(analysis.active_watch_phrases || []), ...(analysis.repeated_phrases || [])]
        .filter((item, index, all) => item && all.indexOf(item) === index);
      dom.characterFatigued.textContent = tired.length ? tired.slice(0, 4).join('、') : '没有表达过热';
      const dashCounts = analysis.dash_counts || [];
      dom.characterDashes.textContent = dashCounts.length
        ? `近 ${dashCounts.length} 条共 ${dashCounts.reduce((a, b) => a + Number(b || 0), 0)} 处`
        : '还没有样本';
      const audits = data.recent_audits || [];
      dom.characterAudits.innerHTML = audits.length ? audits.map((item) => {
        const hits = item.watch_hits || [];
        const note = [
          `${Number(item.dash_count || 0)} 处破折号`,
          hits.length ? `命中：${hits.join('、')}` : '未命中观察词',
        ].join(' · ');
        return `<div class="diagnostic-row static"><span>消息 #${Number(item.message_id || 0)} · ${esc(item.provider || '')}</span><small>${esc(note)}</small><em>${esc(item.created_at ? formatShortDate(item.created_at) : '')}</em></div>`;
      }).join('') : '<div class="diagnostic-empty">保存新回复后，这里会出现只读表达自检；模型原文不会被修改。</div>';
      renderPersonaLab(data.lab || {});
    } catch (e) {
      dom.characterNativeSummary.textContent = `读取失败：${e.message}`;
      if (dom.personaLabIssues) dom.personaLabIssues.innerHTML = `<div class="persona-lab-empty">检查失败：${esc(e.message)}</div>`;
    } finally {
      if (dom.btnCharacterLab) {
        dom.btnCharacterLab.disabled = false;
        dom.btnCharacterLab.textContent = '重新检查';
      }
    }
  }

  async function saveCharacterSettings() {
    if (!dom.btnCharacterSave) return;
    dom.btnCharacterSave.disabled = true;
    try {
      const phrases = (dom.watchPhrases?.value || '').split(/[，,\n]/).map((x) => x.trim()).filter(Boolean);
      const resp = await fetch('/api/character/settings', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          enabled: boolValue(dom.characterEnabled),
          phrase_fatigue: boolValue(dom.phraseFatigue),
          punctuation_fatigue: boolValue(dom.punctuationFatigue),
          watch_phrases: phrases,
        }),
      });
      if (!resp.ok) throw new Error('保存失败');
      await loadCharacterData();
    } catch (e) {
      alert(`性格设置保存失败：${e.message}`);
    } finally {
      dom.btnCharacterSave.disabled = false;
    }
  }

  // ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  //  v6.8 三合一内心 / 独立晨间 / v5.9 活体节律
  // ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  function attachIntimacyVitalsToMessage(messageEl = null) {
    if (!dom.intimacyVitals || !dom.messages) return null;
    const currentAnchor = dom.intimacyVitals.closest('.message.assistant');
    const requestedAnchor = messageEl?.matches?.('.message.assistant')
      ? messageEl
      : null;
    const latestAnchor = [...dom.messages.querySelectorAll('.message.assistant')].at(-1) || null;
    const anchor = requestedAnchor?.isConnected
      ? requestedAnchor
      : (currentAnchor?.isConnected ? currentAnchor : latestAnchor);
    const host = anchor?.querySelector('.message-state-host') || null;
    if (!host) return null;
    if (dom.intimacyVitals.parentElement !== host) host.prepend(dom.intimacyVitals);
    return host;
  }

  function renderIntimacyVitals(data = {}, messageEl = null) {
    state.intimacyVitals = data;
    if (!dom.intimacyVitals) return;
    const sessionMatches = !data.session_id
      || !state.currentSession
      || data.session_id === state.currentSession
      || data.source === '晨间反应';
    const shouldShow = Boolean(
      data.visible && sessionMatches && state.currentView === 'chat'
    );
    const previousHost = dom.intimacyVitals.parentElement;
    const host = shouldShow ? attachIntimacyVitalsToMessage(messageEl) : null;
    const visible = Boolean(shouldShow && host);
    const anchorChanged = Boolean(visible && previousHost !== host);
    const visibilityChanged = dom.intimacyVitals.hidden === visible;
    dom.intimacyVitals.hidden = !visible;
    document.documentElement.classList.toggle('intimacy-vitals-visible', visible);
    if (visibilityChanged || anchorChanged) {
      window.dispatchEvent(new CustomEvent('daxigua:glass-targets-change'));
    }
    if (!visible) return;

    const hardness = Math.max(1, Math.min(10, Number(data.hardness?.level || 1)));
    const arousal = Math.max(1, Math.min(10, Number(data.arousal?.level || 1)));
    const urge = Math.max(0, Math.min(100, Number(data.release_urge?.percent || 0)));
    const tone = String(data.color?.tone || 'natural');
    dom.intimacyVitals.dataset.tone = ['natural', 'rose', 'red', 'crimson', 'purple'].includes(tone)
      ? tone : 'natural';
    if (dom.intimacyVitalsSource) dom.intimacyVitalsSource.textContent = data.source || '亲密过程';
    if (dom.intimacyVitalsSize) {
      dom.intimacyVitalsSize.innerHTML = `${Number(data.size_cm || 22).toFixed(1)}<em> cm</em>`;
    }
    if (dom.intimacyVitalsColor) dom.intimacyVitalsColor.textContent = data.color?.label || '自然';
    if (dom.intimacyVitalsSwelling) dom.intimacyVitalsSwelling.textContent = data.swelling?.label || '轻微充血';
    if (dom.intimacyVitalsArousal) dom.intimacyVitalsArousal.textContent = `${arousal}/10`;
    if (dom.intimacyVitalsHardness) dom.intimacyVitalsHardness.textContent = `${hardness}/10`;
    if (dom.intimacyVitalsUrge) dom.intimacyVitalsUrge.textContent = `${Math.round(urge)}%`;
    const volume = Number(data.ejaculate_ml);
    const hasVolume = data.ejaculate_ml !== null
      && data.ejaculate_ml !== undefined
      && Number.isFinite(volume);
    if (dom.intimacyVitalsVolume) {
      dom.intimacyVitalsVolume.textContent = hasVolume
        ? `${volume.toFixed(1)} mL`
        : (data.ejaculate_label || '尚未射出');
      dom.intimacyVitalsVolume.classList.toggle('has-volume', hasVolume);
    }
    if (dom.intimacyVitalsPhase) {
      dom.intimacyVitalsPhase.textContent = data.phase_label || '升温';
    }
    if (dom.intimacyVitalsCount) {
      dom.intimacyVitalsCount.textContent = `${Math.max(0, Number(data.release_count || 0))} 次`;
    }
    if (dom.intimacyVitalsRefractory) {
      const labels = { early: '不应期', deep: '深不应期', recovering: '恢复中', recovered: '已恢复', none: '未进入不应期' };
      dom.intimacyVitalsRefractory.textContent = labels[String(data.refractory_stage || 'none')]
        || (data.refractory_active ? '不应期' : '已恢复');
    }
    if (dom.intimacyVitalsCycle) {
      dom.intimacyVitalsCycle.textContent = data.eventide?.cycle_label || '平稳期';
    }
    if (dom.intimacyVitalsEvent) {
      dom.intimacyVitalsEvent.textContent = data.eventide?.active_event_label || '无';
    }
  }

  async function loadIntimacyVitals() {
    const session = state.sessions.find((item) => item.id === state.currentSession);
    if (!state.currentSession || session?.persisted === false) {
      renderIntimacyVitals({ visible: false });
      return;
    }
    const requestedSession = state.currentSession;
    const data = await fetchJSON(
      `/api/intimacy/vitals?session_id=${encodeURIComponent(requestedSession)}`,
      { cache: 'no-store' },
    );
    if (requestedSession !== state.currentSession) return;
    renderIntimacyVitals(data);
  }

  async function loadInnerStateData() {
    if (!dom.innerDomainBars) return;
    if (dom.btnInnerRefresh) {
      dom.btnInnerRefresh.disabled = true;
      dom.btnInnerRefresh.textContent = '读取中…';
    }
    try {
      const innerStateUrl = state.currentSession ? `/api/inner-state?session_id=${encodeURIComponent(state.currentSession)}` : '/api/inner-state';
      const data = await fetchJSON(innerStateUrl, { cache: 'no-store' });
      state.innerState = data;
      renderInnerState(data);
      renderIntimacyVitals(data.intimacy_vitals || state.intimacyVitals || {});
    } catch (error) {
      dom.innerDomainSummary.textContent = `读取失败：${error.message}`;
    } finally {
      if (dom.btnInnerRefresh) {
        dom.btnInnerRefresh.disabled = false;
        dom.btnInnerRefresh.textContent = '刷新此刻';
      }
    }
  }

  function renderInnerState(data = {}) {
    const domains = data.domains || {};
    const domain = domains[state.activeInnerDomain] || domains.emotion || { items: [], top: [] };
    document.querySelectorAll('[data-inner-domain]').forEach((button) => {
      button.classList.toggle('active', button.dataset.innerDomain === state.activeInnerDomain);
    });
    const top = Array.isArray(domain.top) ? domain.top : [];
    dom.innerDomainSummary.textContent = top.length
      ? top.slice(0, 3).map((item) => `${item.label} ${item.level}/10`).join(' · ')
      : '此刻很平稳';
    dom.innerDomainBars.innerHTML = (domain.items || []).map((item) => `
      <article class="inner-level-item level-${Number(item.level || 1)}">
        <div><span>${esc(item.label || '')}</span><strong>${Number(item.level || 1)}<small>/10</small></strong></div>
        <div class="inner-ten-track" aria-label="${esc(item.label || '')} ${Number(item.level || 1)}/10">
          ${Array.from({ length: 10 }, (_, index) => `<i class="${index < Number(item.level || 1) ? 'filled' : ''}"></i>`).join('')}
        </div>
        <p>${esc(item.description || '')}</p>
      </article>`).join('');

    const regulation = data.regulation || {};
    if (dom.innerTakeoverLevel) {
      dom.innerTakeoverLevel.textContent = `${Number(regulation.level || 1)}/10`;
    }
    if (dom.innerTakeoverNote) {
      dom.innerTakeoverNote.textContent = `${regulation.label || '稳定'} · 自控 ${Number(regulation.self_control_level || 1)}/10`;
    }

    const intimacy = data.intimacy || {};
    const intimacyLevels = intimacy.levels || {};
    if (dom.innerIntimacyPhase) {
      dom.innerIntimacyPhase.textContent = intimacy.phase_label || '平静';
    }
    if (dom.innerIntimacyNote) {
      dom.innerIntimacyNote.textContent = intimacy.active
        ? `想要 ${Number(intimacyLevels.desire || 1)}/10 · 身体夺权 ${Number(intimacyLevels.body_takeover || 1)}/10`
        : (intimacy.boundary_status === 'paused' || intimacy.boundary_status === 'stopped'
          ? '已暂停推进，等待新的明确靠近'
          : '此刻没有进入亲密状态');
    }

    const morning = data.morning || {};
    const morningEvent = morning.active || morning.last_event;
    const morningLevels = morningEvent?.levels || {};
    const morningDispatch = morningEvent?.dispatch || {};
    const morningDispatchLabel = ({
      pending: '待进入主动链路',
      queued: '已排队',
      recheck: '稍后重试',
      retry: '线路重试中',
      delivered: '已主动表达',
      waited: '模型选择等待',
      superseded: '被新对话接管',
      error: '发送失败',
    })[morningDispatch.proactive_state] || '未排队';
    if (dom.innerMorningLevel) {
      dom.innerMorningLevel.textContent = morningEvent
        ? `Lv.${Number(morningLevels.hardness || 1)}`
        : '未发生';
    }
    if (dom.innerMorningNote) {
      dom.innerMorningNote.textContent = morningEvent
        ? `硬度 ${Number(morningLevels.hardness || 1)} · 主观想要 ${Number(morningLevels.desire || 1)} · ${morningDispatchLabel}`
        : '硬度与主观性欲会分开演化';
    }

    const innerOs = Array.isArray(data.inner_os) ? data.inner_os[0] : null;
    if (dom.innerOsSummary) {
      dom.innerOsSummary.textContent = innerOs?.dominant_emotion || '安静';
    }
    if (dom.innerOsNote) {
      dom.innerOsNote.textContent = innerOs?.content || '不是隐藏思考链，也不进入事实记忆';
    }

    const special = (data.special_events || [])[0];
    if (special) {
      const metrics = (special.metrics || []).map((item) => `
        <div><span>${esc(item.label || '')}</span><b>${Number(item.level || 1)}/10</b></div>`).join('');
      dom.innerSpecialEvent.innerHTML = `
        <details class="inner-special-details" ${special.key === 'morning_response' ? 'open' : ''}>
          <summary><span>${esc(special.label || '特殊事件')}</span><strong>Lv.${Number(special.level || 1)}</strong><b>⌄</b></summary>
          <p>${esc(special.description || '')}</p>
          ${metrics ? `<div class="inner-special-metrics">${metrics}</div>` : ''}
          <small>${special.key === 'morning_response'
            ? (special.caught_up
              ? '程序稍晚启动，已经补算今早状态；旧状态不会补发。'
              : `由本机节律生成并保存在时间线 · ${esc(morningDispatchLabel)}${morningDispatch.proactive_outcome ? ` · ${esc(morningDispatch.proactive_outcome)}` : ''}`)
            : '由本机节律生成并保存在时间线。'}</small>
        </details>`;
    } else {
      dom.innerSpecialEvent.innerHTML = '<div class="inner-event-empty"><span>○</span><p>今天还没有需要展开的身体事件。</p></div>';
    }

    const changes = data.highlights || [];
    dom.innerChangeFeed.innerHTML = changes.length ? changes.map((item) => `
      <div class="inner-change-row ${Number(item.delta || 0) < 0 ? 'down' : 'up'}">
        <i>${Number(item.delta || 0) < 0 ? '↓' : '↑'}</i>
        <span><strong>${esc(item.label || '')}</strong><small>${esc(item.description || item.reason || '')}</small></span>
        <em>${Number(item.before || 1)} → ${Number(item.after || 1)}</em>
      </div>`).join('') : '<div class="inner-event-empty"><span>≈</span><p>最近没有跨级变化。</p></div>';

    const settings = data.settings || {};
    if (dom.innerVisibleChanges) dom.innerVisibleChanges.checked = settings.visible_changes !== false;
    if (dom.innerDetailMode) dom.innerDetailMode.value = settings.detail_mode || 'balanced';
    if (dom.innerEmotionTakeover) dom.innerEmotionTakeover.checked = settings.emotion_takeover_enabled !== false;
    if (dom.innerIntimacyEnabled) dom.innerIntimacyEnabled.checked = settings.intimacy_enabled !== false;
    if (dom.innerIntimacyVitalsVisible) {
      dom.innerIntimacyVitalsVisible.checked = settings.intimacy_vitals_visible !== false;
    }
    if (dom.innerReflectionMode) dom.innerReflectionMode.value = 'local';
    if (dom.innerIntimacyMode) dom.innerIntimacyMode.value = settings.intimacy_mode || 'natural';
    if (dom.innerOsMode) dom.innerOsMode.value = settings.inner_os_mode || 'live';
    if (dom.innerMorningMode) dom.innerMorningMode.value = morning.settings?.mode || 'natural';
    if (dom.innerMorningTakeover) {
      dom.innerMorningTakeover.checked = morning.settings?.body_takeover_enabled !== false;
    }
    if (dom.innerMorningProactive) {
      dom.innerMorningProactive.checked = morning.settings?.proactive_enabled !== false;
    }
  }

  async function saveInnerStateSettings() {
    try {
      const data = await fetchJSON('/api/inner-state/settings', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          session_id: state.currentSession || '',
          visible_changes: Boolean(dom.innerVisibleChanges?.checked),
          detail_mode: dom.innerDetailMode?.value || 'balanced',
          emotion_takeover_enabled: Boolean(dom.innerEmotionTakeover?.checked),
          intimacy_enabled: Boolean(dom.innerIntimacyEnabled?.checked),
          intimacy_vitals_visible: Boolean(dom.innerIntimacyVitalsVisible?.checked),
          reflection_mode: 'local',
          intimacy_mode: dom.innerIntimacyMode?.value || 'natural',
          inner_os_mode: dom.innerOsMode?.value || 'live',
          morning: {
            mode: dom.innerMorningMode?.value || 'natural',
            body_takeover_enabled: Boolean(dom.innerMorningTakeover?.checked),
            proactive_enabled: Boolean(dom.innerMorningProactive?.checked),
          },
        }),
      });
      state.innerState = data;
      renderInnerState(data);
      renderIntimacyVitals(data.intimacy_vitals || {});
    } catch (error) {
      alert(`内在状态设置保存失败：${error.message}`);
    }
  }

  async function loadLivingData() {
    if (!dom.livingPhase) return;
    try {
      const resp = await fetch('/api/living/state', { cache: 'no-store' });
      const data = await resp.json();
      renderLivingState(data);
    } catch (e) {
      dom.livingPhase.textContent = `读取失败：${e.message}`;
    }
  }

  function renderLivingState(data) {
    const settings = data.settings || {};
    dom.livingPhase.textContent = data.phase_label || '平稳';
    const local = data.local_time ? new Date(data.local_time) : null;
    dom.livingTime.textContent = local && !Number.isNaN(local.getTime()) ? local.toLocaleString('zh-CN') : '';
    dom.livingEvent.textContent = data.active_event?.label || '目前平稳';
    dom.livingActivity.textContent = data.activity?.label || '安静待着';
    if (!state.coPresence && dom.livingContacts) {
      dom.livingContacts.textContent = '安静共处';
    }
    dom.livingEnabled.value = String(settings.enabled !== false);
    dom.livingDreams.value = String(settings.dreams_enabled !== false);
    dom.livingMorning.value = settings.morning_response_mode || 'natural';

    const body = data.body || {};
    const labels = data.body_labels || {};
    dom.livingBodyBars.innerHTML = Object.entries(body).map(([key, value]) => `
      <div class="core-bar-row">
        <span>${esc(labels[key] || key)}</span>
        <div class="core-track"><i style="width:${tenLevel(value) * 10}%"></i></div>
        <em>${tenLevel(value)}/10</em>
      </div>`).join('');

    const social = data.social || {};
    const socialLabels = { connection: '连接需要', pride: '骄傲防线', immersion: '沉浸' };
    dom.livingSocialBars.innerHTML = Object.entries(social).map(([key, value]) => {
      const level = key === 'pride' ? tenLevel(Math.max(0, Number(value || 0))) : tenLevel(value);
      return `<div class="core-bar-row social-${esc(key)}"><span>${esc(socialLabels[key] || key)}</span><div class="core-track"><i style="width:${level * 10}%"></i></div><em>${level}/10</em></div>`;
    }).join('');

    const timeline = data.timeline || [];
    dom.livingTimeline.innerHTML = timeline.length ? timeline.map((item) => `
      <div class="diagnostic-row static">
        <span>${esc(item.summary || item.event_type || '')}</span>
        <small>${esc(item.event_type || '')}${item.visibility === 'private' ? ' · 内心事件' : ''}</small>
        <em>${esc(item.created_at ? formatShortDate(item.created_at) : '')}</em>
      </div>`).join('') : '<div class="diagnostic-empty">共同时间线还很安静。运行中的活动、梦和相处余波会留在这里。</div>';
  }

  async function saveLivingSettings() {
    try {
      const resp = await fetch('/api/living/settings', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          enabled: boolValue(dom.livingEnabled),
          dreams_enabled: boolValue(dom.livingDreams),
          morning_response_mode: dom.livingMorning?.value || 'natural',
        }),
      });
      if (!resp.ok) throw new Error('保存失败');
      renderLivingState(await resp.json());
    } catch (e) {
      alert(`活体节律设置保存失败：${e.message}`);
    }
  }

  async function tickLivingState() {
    if (!dom.btnLivingTick) return;
    dom.btnLivingTick.disabled = true;
    dom.btnLivingTick.textContent = '推进中…';
    try {
      const resp = await fetch('/api/living/tick', { method: 'POST' });
      const data = await resp.json();
      renderLivingState(data.state || data);
    } finally {
      dom.btnLivingTick.disabled = false;
      dom.btnLivingTick.textContent = '现在推进一拍';
    }
  }

  async function resetLivingState() {
    if (!confirm('重置身体、连接和活动状态吗？共处感知设置会保留。')) return;
    const resp = await fetch('/api/living/reset', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ keep_settings: true }),
    });
    renderLivingState(await resp.json());
  }

  async function loadDiagnosticsData() {
    if (!dom.diagnosticHealth) return;
    try {
      const resp = await fetch('/api/diagnostics/summary', { cache: 'no-store' });
      const data = await resp.json();
      renderDiagnosticSummary(data);
    } catch (e) {
      dom.diagnosticHealth.textContent = `诊断读取失败：${e.message}`;
    }
  }

  function renderDiagnosticSummary(data) {
    const health = data.health || {};
    const provider = health.provider || {};
    const db = health.database || {};
    const memory = health.memory || {};
    const runtime = health.runtime || {};
    const statusText = health.ok ? '正常' : '有异常';
    const errors = data.recent_errors || [];
    const plugins = data.plugins || [];
    dom.diagnosticHealth.innerHTML = `
      <div class="diagnostic-status-line"><strong>${esc(statusText)}</strong><span>${health.ok ? '●' : '!'}</span></div>
      <div><strong>模型：</strong>${esc(provider.name || '—')} · ${provider.api_key_configured ? 'Key 已配置' : '缺少 Key'}</div>
      <div><strong>数据库：</strong>${db.ok ? '正常' : '异常'} · 消息 ${Number(db.counts?.messages || 0)} · 记忆 ${Number(db.counts?.memories || 0)}</div>
      <div><strong>记忆队列：</strong>${Number(memory.queue_size || 0)}/${Number(memory.queue_max || 0)} · ${memory.worker_running ? '运行中' : '停止'}</div>
      <div><strong>错误：</strong>${errors.length} 条 · <strong>插件：</strong>${plugins.filter((x) => x.health === 'ok').length}/${plugins.length || 0} 正常</div>
    `;

    dom.diagnosticErrors.innerHTML = errors.length ? errors.map((item) => `
      <button class="diagnostic-row diagnostic-error" data-json="${encodeURIComponent(JSON.stringify(item))}">
        <span>${esc(item.source || 'error')}</span>
        <small>${esc(item.type || '')} · 详细错误只保存在本机诊断记录</small>
        <em>${esc(item.at || '')}${item.request_id ? ` · ${esc(item.request_id)}` : ''}</em>
      </button>`).join('') : '<div class="diagnostic-empty">没有记录到错误</div>';

    const traces = data.recent_traces || [];
    dom.diagnosticTraces.innerHTML = traces.length ? traces.map((trace) => `
      <button class="diagnostic-row" data-trace-id="${esc(trace.id || '')}">
        <span>${esc(trace.provider || '')} / ${esc(trace.model || '')}</span>
        <small>${esc(trace.status || '')} · ${Number(trace.duration_ms || 0).toFixed(0)}ms · ${Number(trace.stage_count || 0)} stages · ${Number(trace.tool_count || 0)} tools</small>
        <em>${esc(trace.id || '')}</em>
      </button>`).join('') : '<div class="diagnostic-empty">还没有聊天请求轨迹</div>';

    dom.diagnosticErrors.querySelectorAll('[data-json]').forEach((btn) => {
      btn.addEventListener('click', () => showDiagnosticDetail(JSON.parse(decodeURIComponent(btn.dataset.json))));
    });
    dom.diagnosticTraces.querySelectorAll('[data-trace-id]').forEach((btn) => {
      btn.addEventListener('click', () => loadTraceDetail(btn.dataset.traceId));
    });
  }

  async function loadTraceDetail(id) {
    try {
      const resp = await fetch(`/api/diagnostics/traces/${encodeURIComponent(id)}`);
      const data = await resp.json();
      showDiagnosticDetail(data);
    } catch (e) {
      showDiagnosticDetail({ error: e.message });
    }
  }

  function showDiagnosticDetail(data) {
    dom.diagnosticDetail.textContent = JSON.stringify(data, null, 2);
    dom.diagnosticDetail.classList.remove('hidden');
    if (dom.diagnosticExpanded) dom.diagnosticExpanded.open = true;
    const developer = dom.diagnosticDetail.closest('details');
    if (developer) developer.open = true;
    dom.diagnosticDetail.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }

  async function runDiagnosticSelfTest() {
    if (!dom.btnSelfTest) return;
    dom.btnSelfTest.disabled = true;
    dom.btnSelfTest.textContent = '自检中…';
    try {
      const resp = await fetch('/api/diagnostics/self-test', { method: 'POST' });
      const data = await resp.json();
      showDiagnosticDetail(data);
      await loadDiagnosticsData();
    } catch (e) {
      showDiagnosticDetail({ ok: false, error: e.message });
    } finally {
      dom.btnSelfTest.disabled = false;
      dom.btnSelfTest.textContent = '运行自检';
    }
  }

  async function clearDiagnosticErrors() {
    await fetch('/api/diagnostics/errors', { method: 'DELETE' });
    await loadDiagnosticsData();
  }

  function attachIntegrity(messageEl, report) {
    if (!messageEl || !report || messageEl.querySelector('.integrity-toggle')) return;
    const meta = messageEl.querySelector('.msg-meta');
    if (!meta) return;
    const button = document.createElement('button');
    button.className = 'integrity-toggle';
    button.textContent = '脑完整性';
    const pre = document.createElement('pre');
    pre.className = 'message-integrity hidden';
    pre.textContent = formatIntegrity(report, false);
    button.addEventListener('click', () => pre.classList.toggle('hidden'));
    meta.appendChild(document.createTextNode(meta.textContent ? ' · ' : ''));
    meta.appendChild(button);
    messageEl.appendChild(pre);
  }

  function formatIntegrity(report, includeCapabilities = false) {
    const input = report.input || {};
    const usage = report.usage || {};
    const sentOptions = report.sent_options || {};
    const omittedOptions = report.omitted_options || [];
    const lines = [
      `接口：${report.adapter || report.api_family || '未知'}`,
      `模型：${report.actual_model || report.requested_model || '未知'}`,
      `能力来源：${report.capability_source || '未知'}`,
      `实际发送：${Object.keys(sentOptions).length ? JSON.stringify(sentOptions) : '无额外控制参数'}`,
      `估算输入：${Number(input.estimated_input_tokens || 0).toLocaleString()} tokens${input.context_window ? ` / ${Number(input.context_window).toLocaleString()}` : ''}`,
      `停止原因：${report.stop_reason || (report.preview_only ? '仅预检，未调用模型' : '未知')}`,
    ];
    if (usage.input_tokens != null || usage.output_tokens != null) {
      lines.push(`实际用量：输入 ${usage.input_tokens || 0} · 输出 ${usage.output_tokens || 0} · 推理 ${usage.reasoning_tokens || 0}`);
    }
    if (omittedOptions.length) {
      lines.push('未发送：');
      omittedOptions.forEach((item) => lines.push(`- ${item.option}：${item.reason}`));
    }
    const unknown = report.unknown_stream_events || [];
    if (unknown.length) lines.push(`未知流事件：${unknown.join(', ')}`);
    (report.warnings || []).forEach((warning) => lines.push(`⚠ ${warning}`));
    if (includeCapabilities && report.capabilities) {
      lines.push('', '能力身份证：', JSON.stringify(report.capabilities, null, 2));
    }
    return lines.join('\n');
  }

  function attachTrace(messageEl, trace) {
    if (!messageEl || !trace || messageEl.querySelector('.trace-toggle')) return;
    const meta = messageEl.querySelector('.msg-meta');
    if (!meta) return;
    const button = document.createElement('button');
    button.className = 'trace-toggle';
    button.textContent = `运行轨迹 · ${trace.id}`;
    const pre = document.createElement('pre');
    pre.className = 'message-trace hidden';
    pre.textContent = formatTrace(trace);
    button.addEventListener('click', () => pre.classList.toggle('hidden'));
    meta.appendChild(document.createTextNode(meta.textContent ? ' · ' : ''));
    meta.appendChild(button);
    messageEl.appendChild(pre);
  }

  function formatTrace(trace) {
    const lines = [
      `请求 ${trace.id} · ${trace.status} · ${Number(trace.duration_ms || 0).toFixed(0)}ms`,
      ...((trace.stages || []).map((stage) => `${stage.status === 'ok' ? '✓' : '⚠'} ${stage.label || stage.name}${stage.duration_ms == null ? '' : ` · ${Number(stage.duration_ms).toFixed(0)}ms`}`)),
      ...((trace.tools || []).map((tool) => `${tool.ok ? '✓' : '⚠'} 工具 ${tool.name} · ${Number(tool.duration_ms || 0).toFixed(0)}ms`)),
    ];
    const integrity = trace.usage?.integrity;
    if (integrity) lines.push(`🧠 ${integrity.adapter || integrity.api_family} · ${integrity.actual_model || integrity.requested_model} · ${integrity.stop_reason || '完成'}`);
    if (trace.error) lines.push(`错误：${trace.error}`);
    return lines.join('\n');
  }

  function formatDuration(seconds) {
    const s = Number(seconds || 0);
    if (s < 60) return `${s.toFixed(0)}秒`;
    if (s < 3600) return `${(s / 60).toFixed(1)}分钟`;
    return `${(s / 3600).toFixed(1)}小时`;
  }

  function renderSessionCosts(sessions) {
    const list = $('#session-cost-list');
    list.innerHTML = sessions.slice(0, 20).map((s) => `
      <div class="session-cost-row">
        <span class="cost-title">${esc(s.title || '新对话')}</span>
        <span class="cost-amount">$${(s.total_cost || 0).toFixed(4)}</span>
      </div>
    `).join('');
  }

  async function loadProviders(restoreLocal = true) {
    try {
      const resp = await fetch('/api/providers', {
        cache: 'no-store',
        credentials: 'same-origin',
      });
      const providers = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        if (resp.status === 401) window.location.reload();
        throw new Error(providers.error || providers.detail || `Provider 列表读取失败 (${resp.status})`);
      }
      if (!providers || typeof providers !== 'object' || Array.isArray(providers)) {
        throw new Error('Provider 列表格式无效');
      }
      state.providers = providers;
      let serverActive = null;
      for (const [name, conf] of Object.entries(state.providers)) {
        if (conf.active) serverActive = name;
      }
      const savedProvider = restoreLocal ? localStorage.getItem('companion:provider') : null;
      const savedReady = Boolean(savedProvider && state.providers[savedProvider]?.api_key_configured);
      const serverActiveReady = Boolean(serverActive && state.providers[serverActive]?.api_key_configured);
      const firstReady = Object.keys(state.providers).find(
        (name) => state.providers[name]?.api_key_configured,
      );
      state.activeProvider = savedReady
        ? savedProvider
        : (serverActiveReady ? serverActive : (firstReady || serverActive || Object.keys(state.providers)[0]));
      const conf = state.providers[state.activeProvider] || {};
      state.activeModel = savedModelForProvider(state.activeProvider) || conf.selected_model || conf.default_model || '';
      renderProviders(state.providers);
      renderBrainSettings();
      updateModelPill();
      if (conf.api_key_configured && state.activeProvider !== serverActive) {
        await fetch('/api/providers/switch', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ provider: state.activeProvider, model: state.activeModel }),
        });
      }
    } catch (e) {
      console.warn('Provider 列表加载失败:', e);
    }
  }

  async function refreshProviderConfiguration() {
    await loadProviders(false);
    await loadModelsForProvider(state.activeProvider, true);
  }

  // ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  //  v6.0 系统首页 / 关系连续性 / 原文记忆
  // ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  let browserSessionRepairPromise = null;
  async function repairLocalBrowserSession() {
    if (!['127.0.0.1', 'localhost', '::1'].includes(location.hostname)) return false;
    if (!browserSessionRepairPromise) {
      browserSessionRepairPromise = fetch(`/?_jty_browser_boot=${Date.now()}`, {
        cache: 'no-store', credentials: 'same-origin',
      }).then((response) => response.ok).catch(() => false).finally(() => {
        browserSessionRepairPromise = null;
      });
    }
    return browserSessionRepairPromise;
  }

  async function fetchJSON(url, options = {}) {
    const requestOptions = { credentials: 'same-origin', ...options };
    let response = await fetch(url, requestOptions);
    let data = await response.json().catch(() => ({}));
    if (response.status === 401 && data?.code === 'PAIRING_REQUIRED'
        && await repairLocalBrowserSession()) {
      response = await fetch(url, requestOptions);
      data = await response.json().catch(() => ({}));
    }
    if (!response.ok) throw new Error(data.detail || data.error || `请求失败 (${response.status})`);
    return data;
  }

  async function loadHomeData() {
    if (!dom.homeGreeting) return;
    try {
      const data = await fetchJSON('/api/home', { cache: 'no-store' });
      renderHome(data);
    } catch (error) {
      dom.homeHeroNote && (dom.homeHeroNote.textContent = `首页暂时没有接上：${error.message}`);
    }
  }

  function renderHome(data) {
    const hour = new Date().getHours();
    const greeting = hour < 6 ? '夜还很深，家里给你留着灯'
      : hour < 11 ? '早上好，新的一天从这里醒来'
      : hour < 14 ? '中午好，回来歇一会儿吧'
      : hour < 18 ? '下午好，我们还在同一天里'
      : '晚上好，家里一直亮着灯';
    dom.homeGreeting.textContent = greeting;

    const relation = data.relationship || {};
    const chapter = relation.chapter || {};
    dom.homeChapter.textContent = chapter.label || '正在接续';
    dom.homeChapterNote.textContent = chapter.note || '共同经历正在慢慢长出自己的形状。';
    dom.homeInteractions.textContent = Number(relation.meta?.interactions || 0).toLocaleString();
    dom.homeInteractions.title = '已经完成的相处回合';
    const axes = relation.axes || {};
    dom.homeAxisStrip.innerHTML = ['familiarity', 'warmth', 'playfulness', 'steadiness']
      .filter((key) => axes[key])
      .map((key) => `<div class="axis-mini"><span>${esc(axes[key].label)}</span><i><b style="width:${Math.round(Number(axes[key].value || 0) * 100)}%"></b></i></div>`)
      .join('');

    const living = data.living || {};
    dom.homeLifePhase.textContent = living.phase_label || '此刻';
    dom.homeLifeActivity.textContent = living.activity?.label || '安静待着';
    dom.homePresenceLabel.textContent = living.activity?.label || '正在家里生活';
    dom.homeLifeEvent.textContent = living.active_event?.label || living.active_event?.summary || '今天的生活暂时平稳。';
    const inner = data.inner_state || {};
    state.innerState = inner;
    const innerTop = inner.domains?.emotion?.top || [];
    if (dom.homeInnerHighlights) {
      dom.homeInnerHighlights.innerHTML = innerTop.slice(0, 2).map((item) => `
        <span>${esc(item.label || '')}<b>${Number(item.level || 1)}/10</b></span>`).join('');
    }
    if (dom.mobileLifePhase) dom.mobileLifePhase.textContent = living.phase_label || '此刻';
    if (dom.mobileInteractions) dom.mobileInteractions.textContent = `${Number(relation.meta?.interactions || 0).toLocaleString()} 回合`;
    if (dom.mobileSessionCount) dom.mobileSessionCount.textContent = `${Number(data.summary?.session_count || 0).toLocaleString()} 间`;

    const thread = (relation.threads || [])[0];
    if (thread) {
      dom.homeThreadFocus.innerHTML = `<span class="thread-kind">${esc(threadKindLabel(thread.kind))}</span><strong>${esc(thread.title)}</strong><p>${esc(thread.detail || '等着我们自然接回去。')}</p><button type="button" class="thread-resume-home">从这里继续</button>`;
      dom.homeThreadFocus.querySelector('.thread-resume-home')?.addEventListener('click', () => continueThread(thread));
    } else {
      dom.homeThreadFocus.innerHTML = '<span class="soft-symbol">✓</span><strong>此刻没有悬着的事</strong><p>可以安心开始一段新的对话。</p>';
    }

    const sessions = data.sessions || [];
    dom.homeRecentSessions.innerHTML = sessions.length ? sessions.slice(0, 4).map((session) => `
      <button type="button" class="recent-session-card" data-session-id="${esc(session.id)}">
        <span>${esc(session.title || '新对话')}</span>
        <small>${Number(session.message_count || 0)} 条消息 · ${formatShortDate(session.updated_at)}</small>
      </button>`).join('') : '<div class="empty-card-copy">第一间对话房还没有打开。</div>';
    dom.homeRecentSessions.querySelectorAll('[data-session-id]').forEach((button) => {
      button.addEventListener('click', () => switchSession(button.dataset.sessionId, true));
    });

    const provider = data.summary?.provider || state.activeProvider;
    const providerInfo = state.providers[provider] || {};
    dom.homeModel.textContent = `${providerInfo.display_name || provider} · ${data.summary?.model || state.activeModel || '未选择模型'}`;
    dom.homeHealthDot.classList.toggle('ready', Boolean(providerInfo.api_key_configured));
    dom.homeHealthDot.title = providerInfo.api_key_configured ? 'API 已配置' : '还需要配置 API Key';
    if (dom.mobileBrainState) {
      dom.mobileBrainState.textContent = providerInfo.api_key_configured ? '已连接' : '待连接';
      dom.mobileBrainState.classList.toggle('ready', Boolean(providerInfo.api_key_configured));
    }
  }

  async function loadRelationshipData() {
    if (!dom.relationChapter) return;
    try {
      const data = await fetchJSON('/api/relationship/state', { cache: 'no-store' });
      state.relationship = data;
      renderRelationship(data);
    } catch (error) {
      dom.relationThreadList.innerHTML = `<div class="panel-empty">关系空间读取失败：${esc(error.message)}</div>`;
    }
  }

  function renderRelationship(data) {
    const chapter = data.chapter || {};
    dom.relationChapter.textContent = chapter.label || '我们的故事正在接续';
    dom.relationNote.textContent = chapter.note || '关系正在生长。';
    dom.relationInteractions.textContent = `${Number(data.meta?.interactions || 0).toLocaleString()} 次相处`;
    dom.relationLastSettlement.textContent = data.meta?.last_settlement || '平静地在一起';
    const axes = data.axes || {};
    dom.relationAxisList.innerHTML = Object.entries(axes).map(([key, axis]) => `
      <div class="relation-axis ${key === 'tension' ? 'tension' : ''}">
        <span>${esc(axis.label || key)}<small>${esc(axis.tone || '')}</small></span>
        <i><b style="width:${Math.round(Number(axis.value || 0) * 100)}%"></b></i>
      </div>`).join('');

    const settings = data.settings || {};
    setBoolSelect(dom.continuityEnabled, settings.enabled);
    setBoolSelect(dom.continuityAutoThreads, settings.auto_threads);
    setBoolSelect(dom.continuitySharedContext, settings.shared_context);
    setBoolSelect(dom.continuityMoments, settings.moment_capture);
    const foundation = data.foundation || {};
    if (dom.relationshipFoundation) dom.relationshipFoundation.value = foundation.content || '';
    setBoolSelect(dom.foundationEnabled, foundation.enabled !== false);
    if (dom.foundationStrategy) {
      dom.foundationStrategy.textContent = foundation.prompt_strategy?.description || '前 4000 字常驻，其余按当前话题摘取';
    }
    updateFoundationCount();
    if (dom.foundationStatus) {
      dom.foundationStatus.textContent = foundation.updated_at
        ? `已保存在本机 · ${formatShortDate(foundation.updated_at)} · 不依赖后续对话才生效`
        : '尚未保存时，关系仍可从普通对话继续生长。';
    }
    renderRelationshipThreads(data.threads || []);
    renderRelationshipThreadHistory(data.thread_history || []);
    renderSharedWorld(data.shared || []);
    renderRelationshipMoments(data.moments || []);
  }

  function updateFoundationCount() {
    if (!dom.foundationCount) return;
    const chars = (dom.relationshipFoundation?.value || '').length;
    const estimated = Math.ceil(chars / 2.4);
    dom.foundationCount.textContent = `${chars.toLocaleString()} 字 · 全文约 ${estimated.toLocaleString()}t`;
  }

  async function saveRelationshipFoundation() {
    if (!dom.btnFoundationSave) return;
    dom.btnFoundationSave.disabled = true;
    dom.btnFoundationSave.textContent = '正在保存…';
    try {
      const data = await fetchJSON('/api/relationship/foundation', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          content: dom.relationshipFoundation?.value || '',
          enabled: boolValue(dom.foundationEnabled),
          merge: 'replace',
        }),
      });
      if (state.relationship) state.relationship.foundation = data;
      if (dom.foundationStatus) dom.foundationStatus.textContent = '已保存并立即进入下一轮关系上下文；不需要等待后续对话形成。';
      dom.btnFoundationSave.textContent = '已保存';
      updateFoundationCount();
      setTimeout(() => { if (dom.btnFoundationSave) dom.btnFoundationSave.textContent = '保存总记忆'; }, 1400);
    } catch (error) {
      alert(`关系总记忆保存失败：${error.message}`);
      dom.btnFoundationSave.textContent = '保存总记忆';
    } finally {
      dom.btnFoundationSave.disabled = false;
    }
  }

  async function importRelationshipFoundation(event) {
    const file = event.target.files?.[0];
    event.target.value = '';
    if (!file) return;
    dom.btnFoundationImport && (dom.btnFoundationImport.disabled = true);
    if (dom.foundationStatus) dom.foundationStatus.textContent = `正在解析 ${file.name}…`;
    try {
      const form = new FormData();
      form.append('file', file);
      form.append('merge', (dom.relationshipFoundation?.value || '').trim() ? 'append' : 'replace');
      const response = await fetch('/api/relationship/foundation/import', { method: 'POST', body: form });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || data.error || '导入失败');
      const foundation = data.foundation || {};
      if (dom.relationshipFoundation) dom.relationshipFoundation.value = foundation.content || '';
      setBoolSelect(dom.foundationEnabled, foundation.enabled !== false);
      updateFoundationCount();
      if (dom.foundationStatus) {
        dom.foundationStatus.textContent = `已从 ${file.name} 导入 ${Number(data.import?.character_count || 0).toLocaleString()} 字，并立即生效。`;
      }
    } catch (error) {
      if (dom.foundationStatus) dom.foundationStatus.textContent = `导入失败：${error.message}`;
    } finally {
      dom.btnFoundationImport && (dom.btnFoundationImport.disabled = false);
    }
  }

  function threadKindLabel(kind) {
    return ({ unfinished: '未完', promise: '约定', plan: '计划', question: '待说', care: '记挂' })[kind] || '未完';
  }

  function sharedKindLabel(kind) {
    return ({ object: '物件', place: '地点', ritual: '仪式', activity: '玩法', phrase: '暗号', wish: '愿望' })[kind] || '共同';
  }

  function renderRelationshipThreads(items) {
    if (!items.length) {
      dom.relationThreadList.innerHTML = '<div class="panel-empty"><span>◌</span><strong>没有悬着的事</strong><p>新约定出现时，可以自动捕捉，也可以由你亲手放进来。</p></div>';
      return;
    }
    dom.relationThreadList.innerHTML = items.map((item) => `
      <article class="thread-item" data-thread-id="${Number(item.id)}">
        <div class="thread-marker"><span>${esc(threadKindLabel(item.kind))}</span><i></i></div>
        <div class="thread-body"><h3>${esc(item.title)}</h3><p>${esc(item.detail || '等着自然接续。')}</p>${item.source_excerpt ? `<blockquote>“${esc(item.source_excerpt)}”</blockquote>` : ''}<small>${item.created_by === 'auto' ? '由原话捕捉' : '手动保存'} · ${formatShortDate(item.updated_at)}</small></div>
        <div class="thread-actions"><button data-thread-action="continue">继续</button><button data-thread-action="resolve">完成</button><button data-thread-action="pause">暂放</button><button data-thread-action="delete">删除</button></div>
      </article>`).join('');
    dom.relationThreadList.querySelectorAll('[data-thread-action]').forEach((button) => {
      button.addEventListener('click', async () => {
        const item = items.find((entry) => Number(entry.id) === Number(button.closest('[data-thread-id]')?.dataset.threadId));
        if (!item) return;
        if (button.dataset.threadAction === 'continue') return continueThread(item);
        if (button.dataset.threadAction === 'delete') {
          if (!confirm('把这项未完之事彻底删除吗？')) return;
          try {
            await fetchJSON(`/api/relationship/threads/${encodeURIComponent(item.id)}`, { method: 'DELETE' });
            await Promise.all([loadRelationshipData(), loadHomeData()]);
          } catch (error) { alert(`删除失败：${error.message}`); }
          return;
        }
        const status = button.dataset.threadAction === 'resolve' ? 'resolved' : 'paused';
        await updateRelationshipThread(item.id, { status });
      });
    });
  }

  function continueThread(item) {
    dom.input.value = `我们继续「${item.title}」吧。`;
    autoResize(dom.input);
    updateSendState();
    switchView('chat');
    setTimeout(() => dom.input.focus(), 80);
  }

  function renderRelationshipThreadHistory(items) {
    if (!dom.relationThreadHistory) return;
    dom.relationThreadHistoryCount.textContent = String(items.length);
    dom.relationThreadHistory.innerHTML = items.length ? items.map((item) => `
      <article class="thread-history-item" data-history-id="${Number(item.id)}">
        <div><span>${item.status === 'resolved' ? '已完成' : '暂放'}</span><strong>${esc(item.title)}</strong><small>${formatShortDate(item.updated_at)}</small></div>
        <div><button data-history-action="reopen">重新打开</button><button data-history-action="delete">删除</button></div>
      </article>`).join('') : '<div class="diagnostic-empty">这里还没有记录。</div>';
    dom.relationThreadHistory.querySelectorAll('[data-history-action]').forEach((button) => {
      button.addEventListener('click', async () => {
        const item = items.find((entry) => Number(entry.id) === Number(button.closest('[data-history-id]')?.dataset.historyId));
        if (!item) return;
        if (button.dataset.historyAction === 'reopen') return updateRelationshipThread(item.id, { status: 'open' });
        if (!confirm('彻底删除这项记录吗？')) return;
        try {
          await fetchJSON(`/api/relationship/threads/${encodeURIComponent(item.id)}`, { method: 'DELETE' });
          await Promise.all([loadRelationshipData(), loadHomeData()]);
        } catch (error) { alert(`删除失败：${error.message}`); }
      });
    });
  }

  async function updateRelationshipThread(id, patch) {
    try {
      await fetchJSON(`/api/relationship/threads/${encodeURIComponent(id)}`, {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(patch),
      });
      await Promise.all([loadRelationshipData(), loadHomeData()]);
    } catch (error) {
      alert(`未完之事更新失败：${error.message}`);
    }
  }

  function renderSharedWorld(items) {
    if (!items.length) {
      dom.sharedWorldList.innerHTML = '<div class="panel-empty shared-empty"><span>＋</span><strong>共同空间还是空的</strong><p>把一个真正属于我们的地点、物件或仪式放进来。</p></div>';
      return;
    }
    dom.sharedWorldList.innerHTML = items.map((item, index) => `
      <article class="shared-item shared-tone-${(index % 4) + 1}" data-shared-id="${Number(item.id)}">
        <div class="shared-symbol">${['✦', '◒', '⌁', '❋'][index % 4]}</div><span>${esc(sharedKindLabel(item.kind))}</span><h3>${esc(item.title)}</h3><p>${esc(item.description || '属于我们的一个小小存在。')}</p><div class="shared-actions"><button data-shared-action="archive" title="收起">收起</button><button data-shared-action="delete" title="删除">删除</button></div>
      </article>`).join('');
    dom.sharedWorldList.querySelectorAll('[data-shared-action]').forEach((button) => {
      button.addEventListener('click', async () => {
        const id = button.closest('[data-shared-id]')?.dataset.sharedId;
        try {
          if (button.dataset.sharedAction === 'delete') {
            if (!confirm('把它从共同空间彻底删除吗？')) return;
            await fetchJSON(`/api/relationship/shared/${encodeURIComponent(id)}`, { method: 'DELETE' });
          } else {
            await fetchJSON(`/api/relationship/shared/${encodeURIComponent(id)}`, {
              method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ status: 'archived' }),
            });
          }
          await Promise.all([loadRelationshipData(), loadHomeData()]);
        } catch (error) { alert(`共同空间更新失败：${error.message}`); }
      });
    });
  }

  function renderRelationshipMoments(items) {
    dom.relationMomentList.innerHTML = items.length ? items.map((item) => `
      <article class="moment-item"><i></i><div><span>${esc(item.title)}</span><p>${esc(item.summary || '')}</p><small>${formatShortDate(item.created_at)}</small></div></article>`).join('')
      : '<div class="panel-empty"><span>·</span><strong>时间线还很轻</strong><p>明确的约定、第一次与修复会出现在这里。</p></div>';
  }

  async function saveRelationshipSettings() {
    try {
      const data = await fetchJSON('/api/relationship/settings', {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          enabled: boolValue(dom.continuityEnabled),
          auto_threads: boolValue(dom.continuityAutoThreads),
          shared_context: boolValue(dom.continuitySharedContext),
          moment_capture: boolValue(dom.continuityMoments),
        }),
      });
      state.relationship = data;
      renderRelationship(data);
      dom.btnContinuitySave.textContent = '已保存';
      setTimeout(() => { dom.btnContinuitySave.textContent = '保存'; }, 1200);
    } catch (error) { alert(`连续性设置保存失败：${error.message}`); }
  }

  function openContinuityDialog(mode) {
    state.continuityDialogMode = mode === 'shared' ? 'shared' : 'thread';
    const isShared = state.continuityDialogMode === 'shared';
    dom.continuityDialogTitle.textContent = isShared ? '放进共同空间' : '记下一件没说完的事';
    const options = isShared
      ? [['object', '物件'], ['place', '地点'], ['ritual', '仪式'], ['activity', '玩法'], ['phrase', '暗号'], ['wish', '愿望']]
      : [['unfinished', '未完'], ['promise', '约定'], ['plan', '计划'], ['question', '待说'], ['care', '记挂']];
    dom.continuityKind.innerHTML = options.map(([value, label]) => `<option value="${value}">${label}</option>`).join('');
    dom.continuityTitle.value = '';
    dom.continuityDetail.value = '';
    if (typeof dom.continuityDialog.showModal === 'function') dom.continuityDialog.showModal();
    else dom.continuityDialog.setAttribute('open', '');
    setTimeout(() => dom.continuityTitle.focus(), 80);
  }

  function closeContinuityDialog() {
    if (!dom.continuityDialog) return;
    if (typeof dom.continuityDialog.close === 'function') dom.continuityDialog.close();
    else dom.continuityDialog.removeAttribute('open');
  }

  async function submitContinuityDialog(event) {
    event.preventDefault();
    const title = dom.continuityTitle.value.trim();
    if (!title) return dom.continuityTitle.focus();
    const isShared = state.continuityDialogMode === 'shared';
    const endpoint = isShared ? '/api/relationship/shared' : '/api/relationship/threads';
    const payload = isShared
      ? { kind: dom.continuityKind.value, title, description: dom.continuityDetail.value.trim(), source_session_id: state.currentSession || '' }
      : { kind: dom.continuityKind.value, title, detail: dom.continuityDetail.value.trim(), source_session_id: state.currentSession || '' };
    dom.continuitySubmit && (dom.continuitySubmit.disabled = true);
    try {
      await fetchJSON(endpoint, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
      closeContinuityDialog();
      await Promise.all([loadRelationshipData(), loadHomeData()]);
    } catch (error) {
      alert(`保存失败：${error.message}`);
    } finally {
      dom.continuitySubmit && (dom.continuitySubmit.disabled = false);
    }
  }

  async function previewConversationImport(event) {
    const file = event.target.files?.[0];
    event.target.value = '';
    if (!file || !dom.conversationImportPreview) return;
    clearConversationImportPoll();
    state.conversationImportBatchId = null;
    localStorage.removeItem('daxigua:conversation-import-batch');
    state.conversationImportStatus = 'uploading';
    dom.conversationImportPreview.innerHTML = `
      <div class="import-progress-card"><div><strong>正在把 ${esc(file.name)} 安全放进本机暂存区</strong><span id="import-live-note">上传 0%</span></div><div class="import-progress-track"><i id="import-live-bar"></i></div><small>上传完成后会在后台逐个窗口扫描，不会把整份文件读进内存。</small><div class="import-actions"><button id="btn-import-upload-cancel" class="btn-quiet compact" type="button">取消上传</button></div></div>`;
    $('#btn-import-upload-cancel')?.addEventListener('click', () => {
      state.conversationImportUploadRequest?.abort();
    });
    dom.btnConversationImport && (dom.btnConversationImport.disabled = true);
    try {
      const form = new FormData();
      form.append('file', file);
      const data = await uploadConversationArchive(form);
      state.conversationImportBatchId = data.id;
      localStorage.setItem('daxigua:conversation-import-batch', data.id);
      renderConversationImportBatch(data);
      scheduleConversationImportPoll(250);
    } catch (error) {
      state.conversationImportStatus = 'failed';
      dom.conversationImportPreview.innerHTML = `<span class="import-error">没有导入：${esc(error.message)}</span>`;
    } finally {
      if (dom.btnConversationImport) {
        dom.btnConversationImport.disabled = ['uploading', 'queued', 'scanning', 'ready', 'importing']
          .includes(state.conversationImportStatus);
      }
    }
  }

  function uploadConversationArchive(form) {
    return new Promise((resolve, reject) => {
      const request = new XMLHttpRequest();
      let settled = false;
      const finish = (callback) => {
        if (settled) return;
        settled = true;
        if (state.conversationImportUploadRequest === request) {
          state.conversationImportUploadRequest = null;
        }
        callback();
      };
      state.conversationImportUploadRequest = request;
      request.open('POST', '/api/import/conversations/stage');
      request.responseType = 'json';
      request.upload.addEventListener('progress', (event) => {
        if (!event.lengthComputable) return;
        const percent = Math.max(0, Math.min(100, Math.round(event.loaded * 100 / event.total)));
        const note = $('#import-live-note');
        const bar = $('#import-live-bar');
        if (note) note.textContent = `上传 ${percent}% · ${formatBytes(event.loaded)} / ${formatBytes(event.total)}`;
        if (bar) bar.style.width = `${percent}%`;
      });
      request.addEventListener('load', () => {
        let data = request.response;
        if (!data) { try { data = JSON.parse(request.responseText || '{}'); } catch (_) { data = {}; } }
        finish(() => {
          if (request.status >= 200 && request.status < 300) resolve(data);
          else reject(new Error(data.detail || data.error || `上传失败（HTTP ${request.status}）`));
        });
      });
      request.addEventListener('error', () => finish(() => reject(new Error('上传连接中断'))));
      request.addEventListener('abort', () => finish(() => reject(new Error('上传已取消'))));
      request.send(form);
    });
  }

  function clearConversationImportPoll() {
    if (state.conversationImportPollTimer) clearTimeout(state.conversationImportPollTimer);
    state.conversationImportPollTimer = null;
  }

  function scheduleConversationImportPoll(delay = 700) {
    clearConversationImportPoll();
    if (!state.conversationImportBatchId) return;
    state.conversationImportPollTimer = setTimeout(pollConversationImportBatch, delay);
  }

  async function pollConversationImportBatch() {
    const batchId = state.conversationImportBatchId;
    if (!batchId) return;
    try {
      const data = await fetchJSON(`/api/import/conversations/batches/${encodeURIComponent(batchId)}`, { cache: 'no-store' });
      if (batchId !== state.conversationImportBatchId) return;
      renderConversationImportBatch(data);
      if (['queued', 'scanning', 'importing', 'preparing'].includes(data.status)) scheduleConversationImportPoll(700);
      if (data.status === 'completed') await Promise.all([loadSessions(), loadMemoryOverview(), loadHomeData()]);
    } catch (error) {
      dom.conversationImportPreview.innerHTML = `<span class="import-error">导入状态读取失败：${esc(error.message)}</span>`;
    }
  }

  function renderConversationImportBatch(data) {
    if (!dom.conversationImportPreview || !data) return;
    const status = data.status || 'queued';
    state.conversationImportStatus = status;
    if (dom.btnConversationImport) {
      dom.btnConversationImport.disabled = ['uploading', 'queued', 'scanning', 'ready', 'importing', 'preparing'].includes(status);
    }
    if (['cancelled', 'reverted', 'failed'].includes(status)) {
      localStorage.removeItem('daxigua:conversation-import-batch');
    }
    const conversations = Number(data.conversation_count || 0);
    const messages = Number(data.message_count || 0);
    const thoughts = Number(data.reasoning_count || 0);
    const imported = Number(data.imported_conversations || 0);
    const percent = status === 'importing' && conversations
      ? Math.min(99, Math.round(imported * 100 / conversations))
      : Number(data.progress_percent || 0);
    const formatLabels = { chatgpt: 'ChatGPT', claude: 'Claude', gemini: 'Gemini / Google', generic: '通用 JSON', text: '文本对话' };
    if (status === 'queued' || status === 'scanning') {
      dom.conversationImportPreview.innerHTML = `
        <div class="import-progress-card"><div><strong>${data.cancel_requested ? '正在停止并清理任务' : '正在后台辨认每一个旧窗口'}</strong><span>${conversations.toLocaleString()} 个窗口 · ${messages.toLocaleString()} 条消息${thoughts ? ` · ${thoughts.toLocaleString()} 条显式思考` : ''}</span></div><div class="import-progress-track"><i style="width:${Math.max(4, percent)}%"></i></div><small>可以留在本页等待；刷新后任务仍可继续查看。ZIP 内的附件和图片会被忽略。</small>${data.cancel_requested ? '' : '<div class="import-actions"><button id="btn-import-cancel" class="btn-quiet compact" type="button">取消任务</button></div>'}</div>`;
    } else if (status === 'ready') {
      dom.conversationImportPreview.innerHTML = `
        <div class="import-summary"><span>${esc(formatLabels[data.source_format] || data.source_format || '通用格式')}</span><strong>${conversations.toLocaleString()} 个窗口 · ${messages.toLocaleString()} 条可见消息${thoughts ? ` · ${thoughts.toLocaleString()} 条显式思考` : ''}</strong><small>${Number(data.character_count || 0).toLocaleString()} 字可见对话${thoughts ? `；${Number(data.reasoning_character_count || 0).toLocaleString()} 字思考会单独存放` : ''}；整份档案不会进入模型上下文</small></div>
        <div class="import-samples">${(data.samples || []).map((item) => `<div><strong>${esc(item.title || '未命名窗口')}</strong><small>${Number(item.messages || 0).toLocaleString()} 条 · ${esc(item.preview || '')}</small></div>`).join('')}</div>
        ${(data.warnings || []).map((warning) => `<p class="import-warning">${esc(warning)}</p>`).join('')}
        <div class="import-actions"><button id="btn-import-cancel" class="btn-quiet compact" type="button">取消并清理</button><button id="btn-import-apply" class="btn-primary compact" type="button">确认恢复这些窗口</button></div>`;
    } else if (status === 'importing') {
      dom.conversationImportPreview.innerHTML = `
        <div class="import-progress-card"><div><strong>${data.cancel_requested ? '正在撤回本批已写入窗口' : '正在写入本机对话列表'}</strong><span>${imported.toLocaleString()} / ${conversations.toLocaleString()} 个窗口</span></div><div class="import-progress-track"><i style="width:${Math.max(4, percent)}%"></i></div><small>重复窗口会自动跳过；现在中止会撤销本批已经写入的窗口。</small>${data.cancel_requested ? '' : '<div class="import-actions"><button id="btn-import-cancel" class="btn-quiet compact" type="button">停止迁移</button></div>'}</div>`;
    } else if (status === 'preparing') {
      dom.conversationImportPreview.innerHTML = `
        <div class="import-progress-card"><div><strong>${data.cancel_requested ? '正在撤回本批已写入窗口' : '正在后台整理旧历史'}</strong><span>${Number(data.imported_messages || 0).toLocaleString()} 条消息</span></div><div class="import-progress-track"><i style="width:100%"></i></div><small>窗口已经写入；现在预先整理旧对话，避免后续几十轮聊天持续偿还 backlog。</small>${data.cancel_requested ? '' : '<div class="import-actions"><button id="btn-import-cancel" class="btn-quiet compact" type="button">停止迁移</button></div>'}</div>`;
    } else if (status === 'completed') {
      dom.conversationImportPreview.innerHTML = `<div class="import-success"><strong>旧窗口已经搬回来了</strong><span>新增 ${Number(data.imported_conversations || 0).toLocaleString()} 个窗口、${Number(data.imported_messages || 0).toLocaleString()} 条消息${Number(data.imported_reasoning_traces || 0) ? `、${Number(data.imported_reasoning_traces).toLocaleString()} 条显式思考` : ''}${Number(data.skipped_duplicates || 0) ? `；跳过 ${Number(data.skipped_duplicates).toLocaleString()} 个重复窗口` : ''}。</span><small>思考只在本机展开查看，不会随历史发送给 Claude、GPT 或其他模型。</small><div class="import-actions">${data.can_undo ? '<button id="btn-import-undo" class="btn-quiet compact" type="button">撤销本批导入</button>' : ''}${data.first_session_id ? '<button id="btn-import-open" class="btn-primary compact" type="button">打开第一个恢复窗口</button>' : ''}</div></div>`;
    } else if (status === 'cancelled') {
      dom.conversationImportPreview.innerHTML = '<span>迁移已经停止，暂存文件也已清理；原来的聊天窗口没有受到影响。</span>';
    } else if (status === 'reverted') {
      dom.conversationImportPreview.innerHTML = `<div class="import-success"><strong>本批窗口已经撤销</strong><span>${esc(data.error || '恢复的窗口已从本机对话列表移除。')}</span><small>原始导出包没有被修改；需要时可以重新导入。</small></div>`;
    } else {
      dom.conversationImportPreview.innerHTML = `<div class="import-error"><strong>这次没有完成迁移</strong><span>${esc(data.error || '无法识别这个导出包')}</span><small>可以检查导出包是否完整，然后重新选择；已存在的窗口不会重复。</small></div>`;
    }
    $('#btn-import-cancel')?.addEventListener('click', cancelConversationImport);
    $('#btn-import-apply')?.addEventListener('click', applyConversationImport);
    $('#btn-import-undo')?.addEventListener('click', undoConversationImport);
    $('#btn-import-open')?.addEventListener('click', async () => {
      await loadSessions();
      if (data.first_session_id) await switchSession(data.first_session_id, true);
    });
  }

  async function applyConversationImport() {
    const batchId = state.conversationImportBatchId;
    if (!batchId || !dom.conversationImportPreview) return;
    const applyButton = $('#btn-import-apply');
    if (applyButton) { applyButton.disabled = true; applyButton.textContent = '准备写入…'; }
    try {
      const response = await fetch(`/api/import/conversations/batches/${encodeURIComponent(batchId)}/apply`, { method: 'POST' });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || data.error || '导入失败');
      renderConversationImportBatch({ ...data, status: 'importing' });
      scheduleConversationImportPoll(250);
    } catch (error) {
      dom.conversationImportPreview.innerHTML += `<p class="import-error">导入失败：${esc(error.message)}</p>`;
      if (applyButton) { applyButton.disabled = false; applyButton.textContent = '重新尝试'; }
    }
  }

  async function cancelConversationImport() {
    const batchId = state.conversationImportBatchId;
    if (!batchId) return;
    clearConversationImportPoll();
    try {
      const response = await fetch(`/api/import/conversations/batches/${encodeURIComponent(batchId)}`, { method: 'DELETE' });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || '取消失败');
      renderConversationImportBatch(data);
      if (data.can_cancel || ['queued', 'scanning', 'importing'].includes(data.status)) scheduleConversationImportPoll(500);
    } catch (error) {
      dom.conversationImportPreview.innerHTML += `<p class="import-error">取消失败：${esc(error.message)}</p>`;
    }
  }

  async function undoConversationImport() {
    const batchId = state.conversationImportBatchId;
    if (!batchId || !confirm('撤销这批恢复的所有窗口吗？这些窗口里后来继续发送的消息也会一起移除；其他会话不受影响，已经形成的长期记忆仍按记忆页规则单独保留。')) return;
    try {
      const response = await fetch(`/api/import/conversations/batches/${encodeURIComponent(batchId)}/restored`, { method: 'DELETE' });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || '撤销失败');
      renderConversationImportBatch(data);
      await Promise.all([loadSessions(), loadMemoryOverview(), loadHomeData()]);
      if (!state.sessions.some((item) => item.id === state.currentSession)) {
        if (state.sessions.length) await switchSession(state.sessions[0].id, true);
        else newSession(true);
      }
    } catch (error) {
      alert(`撤销导入失败：${error.message}`);
    }
  }

  async function resumeConversationImport() {
    const batchId = localStorage.getItem('daxigua:conversation-import-batch');
    if (!batchId || !dom.conversationImportPreview) return;
    state.conversationImportBatchId = batchId;
    try {
      const data = await fetchJSON(`/api/import/conversations/batches/${encodeURIComponent(batchId)}`, { cache: 'no-store' });
      renderConversationImportBatch(data);
      if (['queued', 'scanning', 'importing'].includes(data.status)) scheduleConversationImportPoll(700);
    } catch (_) {
      localStorage.removeItem('daxigua:conversation-import-batch');
      state.conversationImportBatchId = null;
    }
  }

  async function loadMemoryOverview() {
    const tasks = [];
    if (dom.memoryCount) {
      tasks.push(
        fetchJSON('/api/memory/archive?query=&limit=1', { cache: 'no-store' })
          .then((data) => {
            dom.memoryCount.textContent = Number(data.stats?.raw_messages || 0).toLocaleString();
          })
          .catch(() => { dom.memoryCount.textContent = '—'; }),
      );
    }
    if (dom.naturalMemoryList) tasks.push(loadNaturalFacts());
    if (dom.favoriteResults) tasks.push(loadFavoriteMessages());
    if (dom.styleProfileEditor) tasks.push(loadStyleProfile());
    await Promise.all(tasks);
  }

  function startBrowserDownload(url) {
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.rel = 'noopener';
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
  }

  function downloadConversationExport(all = false) {
    if (!all && !state.currentSession) {
      alert('先打开一个已经保存的聊天窗口。');
      return;
    }
    const endpoint = all
      ? '/api/export/conversations?format=json'
      : `/api/export/conversations/${encodeURIComponent(state.currentSession)}?format=json`;
    startBrowserDownload(endpoint);
  }

  async function openGlobalHistoryResult(item) {
    if (!item?.session_id) return;
    const opened = await switchSession(String(item.session_id), true);
    if (!opened || String(item.session_id) !== state.currentSession) return;
    const result = await loadHistoryWindowAtPosition(Number(item.history_position || 1));
    if (!result.ok && result.reason === 'error') {
      alert(`消息定位失败：${result.error?.message || '读取失败'}`);
    }
  }

  function renderGlobalHistoryResults(target, items, emptyText) {
    if (!target) return;
    if (!items.length) {
      target.innerHTML = `<div class="panel-empty">${esc(emptyText)}</div>`;
      return;
    }
    target.innerHTML = items.map((item) => `
      <button type="button" class="local-history-result"
        data-history-session="${esc(item.session_id || '')}"
        data-history-position="${Number(item.history_position || 1)}">
        <strong>${esc(item.session_title || '新对话')} · ${item.role === 'user' ? '你' : esc(companionName)}</strong>
        <span>${esc(item.snippet || '')}</span>
        <small>第 ${Number(item.history_position || 1).toLocaleString()} 条 · ${formatShortDate(item.created_at)}${item.note ? ` · ${esc(item.note)}` : ''}</small>
      </button>`).join('');
    target.querySelectorAll('[data-history-session]').forEach((button) => {
      button.addEventListener('click', () => openGlobalHistoryResult({
        session_id: button.dataset.historySession,
        history_position: Number(button.dataset.historyPosition || 1),
      }));
    });
  }

  async function searchAllMessages() {
    const query = dom.chatSearchQuery?.value.trim() || '';
    if (!query) {
      renderGlobalHistoryResults(dom.chatSearchResults, [], '先输入想找的词或句子。');
      return;
    }
    dom.chatSearchResults.innerHTML = '<div class="panel-empty">正在搜索全部聊天…</div>';
    try {
      const data = await fetchJSON(`/api/search/messages?q=${encodeURIComponent(query)}&limit=60`, { cache: 'no-store' });
      renderGlobalHistoryResults(dom.chatSearchResults, data.items || [], '所有窗口中都没有找到。');
    } catch (error) {
      dom.chatSearchResults.innerHTML = `<div class="panel-empty">搜索失败：${esc(error.message)}</div>`;
    }
  }

  async function loadFavoriteMessages() {
    if (!dom.favoriteResults) return;
    try {
      const data = await fetchJSON('/api/favorites?limit=100', { cache: 'no-store' });
      renderGlobalHistoryResults(dom.favoriteResults, data.items || [], '还没有收藏消息。');
    } catch (error) {
      dom.favoriteResults.innerHTML = `<div class="panel-empty">收藏读取失败：${esc(error.message)}</div>`;
    }
  }

  function renderStyleProfile(profile = {}) {
    const exists = Boolean(profile.exists);
    dom.styleProfileEmpty?.classList.toggle('hidden', exists);
    dom.styleProfileEditor?.classList.toggle('hidden', !exists);
    if (!exists) return;
    dom.styleProfileName.value = profile.name || '我的聊天风格';
    dom.styleProfileEnabled.checked = Boolean(profile.enabled);
    dom.styleProfileInstructions.value = profile.instructions || '';
    if (dom.btnStyleUndo) dom.btnStyleUndo.disabled = Number(profile.revision_count || 0) <= 0;
    if (dom.styleProfileExamples) {
      dom.styleProfileExamples.innerHTML = (profile.examples || []).map((item) => (
        `<blockquote>${esc(item.text || '')}</blockquote>`
      )).join('');
    }
  }

  async function loadStyleProfile() {
    if (!dom.styleProfileEditor) return;
    try { renderStyleProfile(await fetchJSON('/api/style-profile', { cache: 'no-store' })); }
    catch (error) { dom.styleProfileEmpty.textContent = `风格档案读取失败：${error.message}`; }
  }

  async function createStyleProfileFromCurrent() {
    if (!state.currentSession || state.sessions.find((item) => item.id === state.currentSession)?.persisted === false) {
      alert('先打开一个已经有助手回复的聊天窗口。');
      return;
    }
    if (!confirm('用当前窗口的助手回复重新提炼风格吗？样本事实不会进入记忆。')) return;
    try {
      const data = await fetchJSON('/api/style-profile/from-session', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: state.currentSession, name: '我的聊天风格' }),
      });
      renderStyleProfile(data);
    } catch (error) { alert(`风格提炼失败：${error.message}`); }
  }

  async function saveStyleProfile() {
    try {
      const data = await fetchJSON('/api/style-profile', {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: dom.styleProfileName?.value || '我的聊天风格',
          instructions: dom.styleProfileInstructions?.value || '',
          enabled: Boolean(dom.styleProfileEnabled?.checked),
        }),
      });
      renderStyleProfile(data);
    } catch (error) { alert(`风格保存失败：${error.message}`); }
  }

  async function undoStyleProfile() {
    try { renderStyleProfile(await fetchJSON('/api/style-profile/undo', { method: 'POST' })); }
    catch (error) { alert(`撤销失败：${error.message}`); }
  }

  async function deleteStyleProfile() {
    if (!confirm('删除这份风格档案吗？')) return;
    try {
      await fetchJSON('/api/style-profile', { method: 'DELETE' });
      await loadStyleProfile();
    } catch (error) { alert(`删除失败：${error.message}`); }
  }

  function downloadLocalBackup() {
    if (dom.btnLocalBackup) {
      dom.btnLocalBackup.textContent = '正在制作备份…';
      setTimeout(() => { dom.btnLocalBackup.textContent = '下载完整备份'; }, 3000);
    }
    startBrowserDownload('/api/local-data/backup');
  }

  async function stageLocalRestore(event) {
    const file = event.target.files?.[0];
    event.target.value = '';
    if (!file || !confirm('确认校验并暂存这个备份吗？系统会先制作回滚备份；当前数据要到下次重启时才会替换。')) return;
    const form = new FormData();
    form.append('file', file);
    if (dom.localRestoreStatus) dom.localRestoreStatus.textContent = '正在校验备份并制作回滚包…';
    try {
      const response = await fetch('/api/local-data/restore', { method: 'POST', body: form });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || '恢复包暂存失败');
      dom.localRestoreStatus.textContent = '备份已校验并暂存。请退出并重新启动大西瓜；API Key 与配对码会保留。';
    } catch (error) {
      dom.localRestoreStatus.textContent = `没有更改数据：${error.message}`;
    }
  }

  async function loadLocalDataStatus() {
    if (!dom.localServiceStatus && !dom.localRestoreStatus) return;
    try {
      const [service, restore] = await Promise.all([
        fetchJSON('/api/local-service', { cache: 'no-store' }),
        fetchJSON('/api/local-data/restore/status', { cache: 'no-store' }),
      ]);
      if (dom.localServiceStatus) {
        dom.localServiceStatus.textContent = service.supported
          ? `${service.loaded ? '常驻正在运行' : service.installed ? '已安装，当前未加载' : '尚未安装'}。${service.sleep_note || ''}`
          : service.detail;
        dom.btnLocalServiceInstall.disabled = !service.supported || service.loaded;
        dom.btnLocalServiceRemove.disabled = !service.supported || !service.installed;
      }
      if (dom.localRestoreStatus) {
        dom.localRestoreStatus.textContent = restore.pending
          ? '已有通过校验的恢复包等待下次重启应用。'
          : restore.last_restore?.ok
            ? `上次恢复已完成：${formatShortDate(restore.last_restore.applied_at)}`
            : '没有等待应用的恢复任务；备份不会包含 API Key 与配对码。';
      }
    } catch (error) {
      if (dom.localServiceStatus) dom.localServiceStatus.textContent = `状态读取失败：${error.message}`;
    }
  }

  async function installLocalService() {
    try {
      await fetchJSON('/api/local-service/install', { method: 'POST' });
      await loadLocalDataStatus();
    } catch (error) { alert(`常驻安装失败：${error.message}`); }
  }

  async function removeLocalService() {
    if (!confirm('移除 macOS 自动启动吗？当前网页在关闭前仍会继续运行。')) return;
    try {
      await fetchJSON('/api/local-service', { method: 'DELETE' });
      await loadLocalDataStatus();
    } catch (error) { alert(`常驻移除失败：${error.message}`); }
  }

  async function loadNaturalFacts() {
    if (!dom.naturalMemoryList) return;
    try {
      const data = await fetchJSON('/api/memory/natural-facts?status=active&limit=100', { cache: 'no-store' });
      const items = Array.isArray(data.items) ? data.items : [];
      if (dom.naturalMemoryCount) {
        dom.naturalMemoryCount.textContent = Number(items.length).toLocaleString();
      }
      renderNaturalFacts(items);
    } catch (error) {
      dom.naturalMemoryList.innerHTML = `<div class="panel-empty">随口记忆读取失败：${esc(error.message)}</div>`;
    }
  }

  function renderNaturalFacts(items) {
    if (!dom.naturalMemoryList) return;
    if (!items.length) {
      dom.naturalMemoryList.innerHTML = '<div class="panel-empty"><span>✦</span><strong>还没有随口记忆</strong><p>自然说“我喜欢吃草莓”就可以；不需要先打开设置，也不必念口令。</p></div>';
      return;
    }
    dom.naturalMemoryList.innerHTML = items.map((item) => {
      const session = item.source_session_id ? `窗口 ${esc(item.source_session_id)}` : '旧窗口';
      const message = item.source_message_id ? ` · 消息 #${Number(item.source_message_id)}` : '';
      const confidence = Number(item.confidence || 0);
      return `
        <article class="natural-memory-item">
          <div class="natural-memory-copy">
            <span>${esc(item.category || 'general')} · ${confidence >= .78 ? '已确认' : '尚不确定'}</span>
            <strong>${esc(item.display_text || item.value || '')}</strong>
            <blockquote>“${esc(item.exact_quote || '')}”</blockquote>
            <small>${session}${message} · ${formatShortDate(item.last_seen_at)}</small>
          </div>
          <div class="natural-memory-actions">
            <button type="button" class="btn-quiet compact" data-edit-natural-memory="${Number(item.id)}" data-memory-text="${encodeURIComponent(String(item.display_text || item.value || ''))}" data-memory-value="${encodeURIComponent(String(item.value || ''))}">修改</button>
            ${Number(item.revision_count || 0) > 0 ? `<button type="button" class="btn-quiet compact" data-undo-natural-memory="${Number(item.id)}">撤销修改</button>` : ''}
            <button type="button" class="btn-quiet compact" data-forget-natural-memory="${Number(item.id)}">忘记</button>
          </div>
        </article>`;
    }).join('');
    dom.naturalMemoryList.querySelectorAll('[data-edit-natural-memory]').forEach((button) => {
      button.addEventListener('click', async () => {
        const factId = Number(button.dataset.editNaturalMemory);
        const next = prompt('把这条记忆改成你确认的说法：', decodeURIComponent(button.dataset.memoryText || ''));
        if (!factId || next === null || !next.trim()) return;
        const nextValue = prompt(
          '这条记忆的核心值是什么？例如“蓝莓”“不吃花生”：',
          decodeURIComponent(button.dataset.memoryValue || ''),
        );
        if (nextValue === null || !nextValue.trim()) return;
        button.disabled = true;
        try {
          await fetchJSON(`/api/memory/natural-facts/${factId}`, {
            method: 'PATCH', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ display_text: next.trim(), value: nextValue.trim() }),
          });
          await loadNaturalFacts();
        } catch (error) {
          button.disabled = false;
          alert(`修改失败：${error.message}`);
        }
      });
    });
    dom.naturalMemoryList.querySelectorAll('[data-undo-natural-memory]').forEach((button) => {
      button.addEventListener('click', async () => {
        const factId = Number(button.dataset.undoNaturalMemory);
        if (!factId) return;
        button.disabled = true;
        try {
          await fetchJSON(`/api/memory/natural-facts/${factId}/undo`, { method: 'POST' });
          await loadNaturalFacts();
        } catch (error) {
          button.disabled = false;
          alert(`撤销失败：${error.message}`);
        }
      });
    });
    dom.naturalMemoryList.querySelectorAll('[data-forget-natural-memory]').forEach((button) => {
      button.addEventListener('click', async () => {
        const factId = Number(button.dataset.forgetNaturalMemory);
        if (!factId || !confirm('只忘记这条随口事实吗？原聊天不会删除。')) return;
        button.disabled = true;
        try {
          await fetchJSON(`/api/memory/natural-facts/${factId}`, { method: 'DELETE' });
          await loadNaturalFacts();
        } catch (error) {
          button.disabled = false;
          alert(`忘记失败：${error.message}`);
        }
      });
    });
  }

  async function searchMemoryArchive() {
    const query = dom.memoryQuery?.value.trim() || '';
    if (!query) {
      dom.memoryResults.innerHTML = '<div class="panel-empty"><span>⌕</span><strong>先写下你记得的一点点</strong><p>一个称呼、半句话或某个项目名都可以。</p></div>';
      return;
    }
    dom.memoryResults.innerHTML = '<div class="memory-loading">正在沿着原话找回去…</div>';
    try {
      const data = await fetchJSON(`/api/memory/archive?query=${encodeURIComponent(query)}&limit=12`, { cache: 'no-store' });
      dom.memoryCount.textContent = Number(data.stats?.raw_messages || 0).toLocaleString();
      renderMemoryResults(data.items || []);
    } catch (error) {
      dom.memoryResults.innerHTML = `<div class="panel-empty">搜索失败：${esc(error.message)}</div>`;
    }
  }

  function renderMemoryResults(items) {
    if (!items.length) {
      dom.memoryResults.innerHTML = '<div class="panel-empty"><span>∅</span><strong>没有找到对应原话</strong><p>可以换一个更接近当时说法的词。</p></div>';
      return;
    }
    dom.memoryResults.innerHTML = items.map((item) => `
      <button type="button" class="memory-result" data-memory-source="${Number(item.message_id)}">
        <span class="memory-role ${item.role === 'user' ? 'user' : 'assistant'}">${item.role === 'user' ? '你' : (item.conversation_deleted ? '来源摘录' : esc(companionName))}</span>
        <blockquote>${esc(item.quote || item.content || '')}</blockquote>
        <small>消息 #${Number(item.message_id)} · ${formatShortDate(item.created_at)} · 相关度 ${Number(item.score || 0).toFixed(1)}${item.conversation_deleted ? ' · 原对话已删除' : ''}</small>
      </button>`).join('');
    dom.memoryResults.querySelectorAll('[data-memory-source]').forEach((button) => {
      button.addEventListener('click', () => expandMemorySource(button));
    });
  }

  async function expandMemorySource(button) {
    const existing = button.querySelector('.memory-source-full');
    if (existing) return existing.classList.toggle('hidden');
    try {
      const data = await fetchJSON(`/api/memory/source/${encodeURIComponent(button.dataset.memorySource)}`);
      const full = document.createElement('div');
      full.className = 'memory-source-full';
      full.textContent = data.conversation_deleted
        ? `完整对话已删除；保留的来源摘录：\n${data.content || ''}`
        : (data.content || '');
      button.appendChild(full);
    } catch (error) { alert(`原文读取失败：${error.message}`); }
  }

  function setBoolSelect(element, value) {
    if (element) element.value = value === false ? 'false' : 'true';
  }

  function formatShortDate(value) {
    if (!value) return '刚刚';
    const parsed = new Date(String(value).replace(' ', 'T') + (String(value).includes('T') ? '' : 'Z'));
    if (Number.isNaN(parsed.getTime())) return String(value).slice(0, 16);
    return new Intl.DateTimeFormat('zh-CN', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }).format(parsed);
  }

  // ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  //  视图切换
  // ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  function clearGlassViewTransition(except = null) {
    if (state.viewTransitionTimer) {
      clearTimeout(state.viewTransitionTimer);
      state.viewTransitionTimer = null;
    }
    $$('.view.glass-view-outgoing').forEach((viewNode) => {
      if (viewNode === except) return;
      viewNode.classList.remove('glass-view-outgoing');
      viewNode.removeAttribute('aria-hidden');
    });
    $$('.view.glass-view-entering').forEach((viewNode) => {
      if (viewNode === except) return;
      viewNode.classList.remove('glass-view-entering');
      if (viewNode.classList.contains('active')) viewNode.classList.add('glass-view-settled');
    });
  }

  function glassViewDirection(view) {
    const trail = Array.isArray(state.viewTrail) && state.viewTrail.length
      ? state.viewTrail
      : [state.currentView || 'home'];
    const previousIndex = trail.lastIndexOf(view);
    if (previousIndex >= 0 && previousIndex < trail.length - 1) {
      state.viewTrail = trail.slice(0, previousIndex + 1);
      return 'back';
    }
    if (trail[trail.length - 1] !== view) {
      state.viewTrail = [...trail, view].slice(-12);
    }
    return 'forward';
  }

  function _switchViewNow(view) {
    const target = $(`#view-${view}`);
    if (!target) return;
    const outgoing = $('.view.active');
    const sameView = outgoing === target;
    const direction = sameView ? 'forward' : glassViewDirection(view);
    const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    clearGlassViewTransition(target);
    state.currentView = view;
    document.documentElement.dataset.activeView = view;
    document.documentElement.dataset.viewMotion = direction;
    $$('.view').forEach((v) => {
      if (v !== outgoing && v !== target) {
        v.classList.remove('active', 'glass-view-entering', 'glass-view-settled');
        v.removeAttribute('aria-hidden');
      }
    });
    if (!sameView && outgoing) {
      // A fixed GPU glass canvas cannot stay visually attached to two animated
      // views at once. Retire the old view immediately; only the incoming view
      // is animated. This removes stale parent-glass rectangles and cross-view
      // stacking during navigation.
      outgoing.classList.remove('active', 'glass-view-entering', 'glass-view-settled', 'glass-view-outgoing');
      outgoing.removeAttribute('aria-hidden');
    }
    target.classList.remove('glass-view-outgoing', 'glass-view-settled');
    target.classList.add('active');
    target.removeAttribute('aria-hidden');
    if (!sameView && !reduceMotion) target.classList.add('glass-view-entering');
    else target.classList.add('glass-view-settled');

    if (!sameView && !reduceMotion) {
      state.viewTransitionTimer = setTimeout(() => {
        target.classList.remove('glass-view-entering');
        target.classList.add('glass-view-settled');
        state.viewTransitionTimer = null;
      }, 540);
    }
    $$('.btn-nav[data-view]').forEach((button) => {
      const active = button.dataset.view === view;
      button.classList.toggle('active', active);
      button.setAttribute('aria-current', active ? 'page' : 'false');
    });
    const titles = { home: '回家', us: '我们', life: '生活', memory: '记忆馆', inner: '内心', system: '工作室' };
    dom.headerTitle.textContent = view === 'chat'
      ? (state.sessions.find((s) => s.id === state.currentSession)?.title || companionName)
      : (titles[view] || companionName);
    localStorage.setItem('daxigua:last-view', view);
    if (view === 'home') loadHomeData();
    if (view === 'us') loadRelationshipData();
    if (view === 'memory') loadMemoryOverview();
    if (view === 'life') Promise.all([loadLivingData(), loadFileWorkspace(), loadOceanListenStatus()]).catch(() => {});
    if (view === 'inner') Promise.all([loadInnerStateData(), loadCoreData(), loadCharacterData(), loadLivingData(), loadDesirePanel()]);
    if (view === 'system') loadConsoleData();
    if (view === 'chat') loadIntimacyVitals().catch(() => {});
    else renderIntimacyVitals(state.intimacyVitals || {});
  }

  const macNativeScrollState = new Map();
  let macViewTransitionBusy = false;
  let macQueuedView = null;

  function isMacDesktopNativeFeel() {
    return window.matchMedia('(min-width: 901px) and (hover: hover) and (pointer: fine)').matches;
  }

  function macScrollableFor(viewNode) {
    if (!viewNode) return null;
    const preferred = viewNode.querySelector('[data-native-scroll], .messages, .console-scroll, .memory-scroll');
    if (preferred && preferred.scrollHeight > preferred.clientHeight) return preferred;
    return Array.from(viewNode.querySelectorAll('*')).find((node) => {
      const style = getComputedStyle(node);
      return /(auto|scroll)/.test(style.overflowY) && node.scrollHeight > node.clientHeight + 8;
    }) || null;
  }

  function rememberMacViewScroll(viewName) {
    if (!isMacDesktopNativeFeel() || !viewName) return;
    const node = document.querySelector(`#view-${viewName}`);
    const scroller = macScrollableFor(node);
    if (scroller) macNativeScrollState.set(viewName, scroller.scrollTop || 0);
  }

  function restoreMacViewScroll(viewName) {
    if (!isMacDesktopNativeFeel()) return;
    const saved = macNativeScrollState.get(viewName);
    if (saved == null) return;
    requestAnimationFrame(() => requestAnimationFrame(() => {
      const scroller = macScrollableFor(document.querySelector(`#view-${viewName}`));
      if (scroller) scroller.scrollTop = Math.max(0, saved);
    }));
  }

  function switchView(view) {
    if (view === 'life') view = 'home';
    const previous = state.currentView || 'home';
    if (!isMacDesktopNativeFeel() || !document.startViewTransition || window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      rememberMacViewScroll(previous);
      _switchViewNow(view);
      restoreMacViewScroll(view);
      return;
    }
    if (macViewTransitionBusy) {
      macQueuedView = view;
      return;
    }
    rememberMacViewScroll(previous);
    macViewTransitionBusy = true;
    document.documentElement.dataset.macNativeTransition = 'true';
    const transition = document.startViewTransition(() => _switchViewNow(view));
    transition.finished.finally(() => {
      delete document.documentElement.dataset.macNativeTransition;
      macViewTransitionBusy = false;
      restoreMacViewScroll(view);
      if (macQueuedView && macQueuedView !== view) {
        const queued = macQueuedView;
        macQueuedView = null;
        switchView(queued);
      } else {
        macQueuedView = null;
      }
    });
  }

  // macOS muscle-memory shortcuts follow the visible navigation (Life now lives inside Home).
  window.addEventListener('keydown', (event) => {
    if (!isMacDesktopNativeFeel() || !event.metaKey || event.altKey || event.ctrlKey) return;
    if (/INPUT|TEXTAREA|SELECT/.test(document.activeElement?.tagName || '')) return;
    const map = { '1':'home', '2':'chat', '3':'us', '4':'memory', '5':'inner', '6':'system' };
    const view = map[event.key];
    if (!view) return;
    event.preventDefault();
    switchView(view);
  });

  function syncFlowerSeaControls() {
    const root = document.documentElement;
    root.dataset.uiMode = 'scenic';
    root.dataset.glassMode = 'view';
    const active = root.dataset.flowerSea === 'true';
    const weatherMode = window.DaxiguaWeather?.target || root.dataset.weather || 'clear';
    $$('.scene-mode-button').forEach((button) => {
      const selected = button.dataset.sceneMode === 'water'
        ? active
        : (!active && button.dataset.sceneMode === weatherMode);
      button.classList.toggle('active', selected);
      button.setAttribute('aria-checked', String(selected));
    });
    if (dom.btnFlowerSea) {
      dom.btnFlowerSea.disabled = false;
      dom.btnFlowerSea.setAttribute('aria-pressed', String(active));
      dom.btnFlowerSea.title = active ? '当前为水波模式' : '切换到水波';
      dom.btnFlowerSea.setAttribute('aria-label', dom.btnFlowerSea.title);
    }
  }

  function setFlowerSea(active) {
    const root = document.documentElement;
    if (active) {
      window.DaxiguaWeather?.setMode?.('clear');
      if ((root.dataset.activeView || 'home') !== 'home') switchView('home');
    }
    root.dataset.flowerSea = String(Boolean(active));
    root.dataset.uiMode = 'scenic';
    root.dataset.glassMode = 'view';
    syncFlowerSeaControls();
    window.dispatchEvent(new CustomEvent('daxigua:flower-sea-change', {
      detail: { active: Boolean(active), water: Boolean(active) },
    }));
    window.requestAnimationFrame(() => window.dispatchEvent(new Event('resize')));
  }

  function activateFlowerSeaMode() {
    if (document.documentElement.dataset.flowerSea === 'true') {
      syncFlowerSeaControls();
      return;
    }
    setFlowerSea(true);
  }

  function syncFontControls() {
    const allowed = new Set(['modern', 'letter', 'system']);
    const saved = document.documentElement.dataset.font
      || localStorage.getItem('daxigua:v651-font')
      || localStorage.getItem('daxigua:v65-font')
      || 'modern';
    const mode = allowed.has(saved) ? saved : 'modern';
    document.documentElement.dataset.font = mode;
    if (dom.fontMode) dom.fontMode.value = mode;
    if (dom.fontPreview) dom.fontPreview.dataset.mode = mode;
  }

  function updateFontMode() {
    const mode = ['modern', 'letter', 'system'].includes(dom.fontMode?.value)
      ? dom.fontMode.value : 'modern';
    document.documentElement.dataset.font = mode;
    localStorage.setItem('daxigua:v651-font', mode);
    syncFontControls();
  }

  function syncTextScale() {
    const allowed = new Set(['standard', 'cozy', 'large']);
    const saved = document.documentElement.dataset.textscale
      || localStorage.getItem('daxigua:v652-textscale')
      || 'standard';
    const mode = allowed.has(saved) ? saved : 'standard';
    document.documentElement.dataset.textscale = mode;
    if (dom.textScale) dom.textScale.value = mode;
  }

  function updateTextScale() {
    const mode = ['standard', 'cozy', 'large'].includes(dom.textScale?.value)
      ? dom.textScale.value : 'standard';
    document.documentElement.dataset.textscale = mode;
    localStorage.setItem('daxigua:v652-textscale', mode);
    syncTextScale();
  }

  function onConsoleNavClick(event) {
    const link = event.target.closest('a[href^="#"]');
    if (!link || !dom.consoleNav) return;
    event.preventDefault();
    const target = document.getElementById(link.getAttribute('href').slice(1));
    if (!target) return;
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    target.scrollIntoView({ behavior: reduced ? 'auto' : 'smooth', block: 'start' });
    dom.consoleNav.querySelectorAll('a').forEach((a) => a.classList.toggle('active', a === link));
  }

  // ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  //  侧边栏
  // ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  function toggleSidebar() {
    dom.sidebar.classList.toggle('open');
    overlay.classList.toggle('show');
  }
  function closeSidebar() {
    dom.sidebar.classList.remove('open');
    overlay.classList.remove('show');
  }

  // ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  //  PWA & Service Worker
  // ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  async function registerSW() {
    if (!('serviceWorker' in navigator)) return;
    try {
      const hadController = Boolean(navigator.serviceWorker.controller);
      let reloadingForUpdate = false;
      navigator.serviceWorker.addEventListener('controllerchange', () => {
        if (!hadController || reloadingForUpdate) return;
        // Never discard an in-progress reply or an unsent private draft just
        // to activate a cache update. Versioned assets already make this page
        // current; an idle page can safely perform the one-time refresh.
        if (state.isStreaming || Boolean(dom.input?.value)) return;
        reloadingForUpdate = true;
        window.location.reload();
      }, { once: true });
      const reg = await navigator.serviceWorker.register(
        `/sw.js?v=${encodeURIComponent(frontendRevision)}`,
        { updateViaCache: 'none' },
      );
      await reg.update().catch(() => {});
      console.log('[SW] 注册成功');
      navigator.serviceWorker.addEventListener('message', (event) => {
        if (event.data?.type === 'switch_session' && event.data.session_id) {
          switchSession(String(event.data.session_id), true);
        }
      });

      // 页面加载时不偷偷弹权限框。已有授权才静默恢复订阅；首次授权必须由
      // “开启系统通知”按钮的真实用户手势触发，iOS/Safari 才稳定接受。
      if ('PushManager' in window && 'Notification' in window
          && Notification.permission === 'granted') {
        await subscribePush(reg);
      }
      await refreshPushStatus();
    } catch (e) {
      console.error('[SW] 注册失败:', e);
    }
  }

  async function subscribePush(reg) {
    try {
      const keyResp = await fetch('/api/push/vapid-key');
      const { publicKey } = await keyResp.json();
      if (!publicKey) return;

      let sub = await reg.pushManager.getSubscription();
      if (!sub) {
        sub = await reg.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: urlBase64ToUint8Array(publicKey),
        });
      }

      await fetch('/api/push/subscribe', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(sub.toJSON()),
      });
      console.log('[Push] 订阅成功');
      await refreshPushStatus();
    } catch (e) {
      console.log('[Push] 订阅失败:', e);
    }
  }

  // ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  //  工具函数
  // ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  function formatBytes(value) {
    const bytes = Number(value || 0);
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  }

  function cssEscape(value) {
    if (window.CSS?.escape) return CSS.escape(String(value || ''));
    return String(value || '').replace(/[^a-zA-Z0-9_-]/g, '');
  }

  function esc(str) {
    const d = document.createElement('div');
    d.textContent = str || '';
    return d.innerHTML;
  }

  function formatNumber(n) {
    if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + 'M';
    if (n >= 1_000) return (n / 1_000).toFixed(1) + 'K';
    return n.toString();
  }

  function autoResize(el) {
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 120) + 'px';
  }

  function urlBase64ToUint8Array(base64) {
    const padding = '='.repeat((4 - (base64.length % 4)) % 4);
    const raw = atob((base64 + padding).replace(/-/g, '+').replace(/_/g, '/'));
    return Uint8Array.from([...raw].map((c) => c.charCodeAt(0)));
  }

  // Same-origin call iframe bridge.  It exposes only the current voice turn,
  // never the private application state or provider credentials.
  window.DaxiguaVoiceChat = Object.freeze({
    companionName: () => companionName,
    currentSessionId: () => {
      if (!state.currentSession) newSession(false);
      return state.currentSession;
    },
    voiceState: async () => state.voice || loadVoiceSettings(),
    isBusy: () => Boolean(state.isStreaming || state.recoveringPendingRequest),
    sendTranscript: async (payload = {}) => sendMessage({
      text: String(payload.text || ''),
      voiceTranscript: true,
      voiceDurationMs: Number(payload.durationMs || 0),
      voiceTranscriber: String(payload.transcriber || 'ElevenLabs'),
      voiceAcoustic: payload.acoustic || {},
      voiceMood: payload.mood || {},
      voicePrivateMode: payload.privateMode === true,
      voiceSleepMode: payload.sleepMode === true,
      suppressAutoVoice: true,
    }),
    setStatus: (text) => {
      if (dom.headerStatus) dom.headerStatus.textContent = String(text || '');
    },
  });

  // ━━ 启动 ━━
  document.addEventListener('DOMContentLoaded', init);

  // ━━━ 它的内心面板 ━━━
  const DRIVE_LABELS = {
    attachment: '想念', curiosity: '好奇', reflection: '沉淀', duty: '记挂',
    social: '人群', libido: '亲密', stress: '压力', fatigue: '疲惫'
  };
  
  async function loadDesirePanel() {
    try {
      const r = await fetch('/api/desire/state');
      const d = await r.json();
  
      const intentEl = document.getElementById('desire-intent');
      if (d.intent) {
        intentEl.innerHTML = `<span class="intent-dot"></span>此刻：${esc(d.intent.reason || '')}` +
          `<span class="intent-score">${(d.intent.score*100|0)}</span>`;
      } else {
        intentEl.innerHTML = `<span class="intent-dot calm"></span>此刻很平静，没什么特别想做的`;
      }
  
      const bars = document.getElementById('desire-bars');
      bars.innerHTML = Object.entries(d.drive).map(([k, v]) => `
        <div class="drive-row${k==='fatigue'?' is-gate':''}">
          <span class="drive-name">${DRIVE_LABELS[k]||k}</span>
          <div class="drive-track"><div class="drive-fill" style="width:${v*100}%"></div></div>
          <span class="drive-val">${tenLevel(v)}/10</span>
        </div>`).join('');
  
      const th = document.getElementById('desire-thoughts');
      th.innerHTML = d.thoughts.length
        ? d.thoughts.slice(0,6).map(t => `
          <div class="thought ${t.kind}">
            <span class="thought-kind">${t.kind==='fixation'?'执念':'闪念'}</span>
            <span class="thought-text">${esc(t.text || '')}</span>
          </div>`).join('')
        : '<div class="thought-empty">念头池空空的</div>';
  
      const gates = document.getElementById('desire-gates');
      gates.innerHTML = Object.entries(d.gates).map(([k, on]) =>
        `<span class="gate ${on?'on':''}">${k.replace('DESIRE_','').replace('HEARTBEAT_','HB_')}</span>`
      ).join('');
    } catch (e) { console.error('desire panel:', e); }
  }
  
  // 钩进控制台加载
  const _origLoadConsole = loadConsoleData;

})();
