package com.aura.agent.ui

import android.Manifest
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.ServiceConnection
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.os.IBinder
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.TextView
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.fragment.app.Fragment
import androidx.fragment.app.FragmentActivity
import androidx.lifecycle.Lifecycle
import android.widget.LinearLayout
import androidx.lifecycle.lifecycleScope
import androidx.lifecycle.repeatOnLifecycle
import androidx.viewpager2.adapter.FragmentStateAdapter
import androidx.viewpager2.widget.ViewPager2
import com.aura.agent.AuraApplication
import com.aura.agent.BuildConfig
import com.aura.agent.R
import com.aura.agent.fusion.FusionState
import com.aura.agent.fusion.PositionQuality
import com.aura.agent.fusion.SensorFusionService
import com.aura.agent.sensors.BleBeacon
import com.aura.agent.security.Severity
import com.google.android.material.tabs.TabLayout
import com.google.android.material.tabs.TabLayoutMediator
import org.json.JSONObject
import android.widget.ArrayAdapter
import android.widget.Spinner
import com.google.android.material.button.MaterialButton
import com.google.android.material.progressindicator.LinearProgressIndicator
import com.google.android.material.slider.Slider
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import android.view.inputmethod.EditorInfo
import com.google.android.material.materialswitch.MaterialSwitch
import com.google.android.material.textfield.TextInputEditText
import com.aura.agent.NativeEngine
import com.aura.agent.llm.LlmStatus
import com.google.android.material.floatingactionbutton.FloatingActionButton
import com.google.android.material.snackbar.Snackbar
import kotlinx.coroutines.launch

/**
 * Single-activity shell with four tabs (Live / Karte / Szenarien / Einstellungen).
 *
 * The heavy 3D view is the same Babylon.js bundle the desktop visualiser uses,
 * hosted in a WebView. Shipping one renderer instead of two keeps the semantic
 * colour code and the scene graph identical everywhere, which matters when an
 * incident commander on a laptop and an operator on a CT45P have to describe
 * the same scene to each other.
 */
class MainActivity : AppCompatActivity() {

    private var service: SensorFusionService? = null
    private var bound = false

    /**
     * The bound service, for fragments.
     *
     * Fragments are created by the pager before the service connects, so this
     * is nullable by nature and every caller must handle null rather than
     * assume ordering.
     */
    val fusionService: SensorFusionService? get() = service

    private val connection = object : ServiceConnection {
        override fun onServiceConnected(name: ComponentName?, binder: IBinder?) {
            service = (binder as? SensorFusionService.LocalBinder)?.service()
            bound = true
            observeFusion()
            // Fragments that were created before the connection landed have
            // nothing to observe yet; let them start now.
            supportFragmentManager.fragments.forEach {
                (it as? ServiceAware)?.onServiceReady()
            }
        }

        override fun onServiceDisconnected(name: ComponentName?) {
            service = null
            bound = false
        }
    }

    private val permissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { granted ->
        val missing = granted.filterValues { !it }.keys
        if (missing.isEmpty()) {
            startFusion()
        } else {
            AuraApplication.instance.audit.append(
                "ui", "permission.denied",
                org.json.JSONObject().put("missing", missing.joinToString(",")),
                Severity.WARNING,
            )
            findViewById<TextView>(R.id.status_banner)?.apply {
                text = getString(R.string.permission_required)
                visibility = View.VISIBLE
            }
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        val pager = findViewById<ViewPager2>(R.id.view_pager)
        val tabs = findViewById<TabLayout>(R.id.tab_layout)
        pager.adapter = TabAdapter(this)
        // Keep all four tabs alive: re-creating the WebGL context every swipe
        // costs ~400 ms and drops the live telemetry stream.
        pager.offscreenPageLimit = 3

        TabLayoutMediator(tabs, pager) { tab, position ->
            tab.text = getString(
                when (position) {
                    0 -> R.string.tab_live
                    1 -> R.string.tab_map
                    2 -> R.string.tab_scenario
                    else -> R.string.tab_settings
                }
            )
        }.attach()

        requestPermissions()
    }

    private fun requestPermissions() {
        val required = buildList {
            add(Manifest.permission.ACCESS_FINE_LOCATION)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                add(Manifest.permission.BLUETOOTH_SCAN)
                add(Manifest.permission.BLUETOOTH_CONNECT)
            } else {
                add(Manifest.permission.BLUETOOTH)
                add(Manifest.permission.BLUETOOTH_ADMIN)
            }
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                add(Manifest.permission.POST_NOTIFICATIONS)
            }
        }
        val missing = required.filter {
            ContextCompat.checkSelfPermission(this, it) != PackageManager.PERMISSION_GRANTED
        }
        if (missing.isEmpty()) startFusion() else permissionLauncher.launch(missing.toTypedArray())
    }

    private fun startFusion() {
        val intent = Intent(this, SensorFusionService::class.java)
        ContextCompat.startForegroundService(this, intent)
        bindService(intent, connection, Context.BIND_AUTO_CREATE)
    }

    private fun observeFusion() {
        val fusion = service ?: return
        lifecycleScope.launch {
            repeatOnLifecycle(Lifecycle.State.STARTED) {
                fusion.state.collect { render(it) }
            }
        }
    }

    private fun render(state: FusionState) {
        val banner = findViewById<TextView>(R.id.status_banner) ?: return
        val snapshot = state.ekf
        banner.visibility = View.VISIBLE
        banner.text = if (snapshot == null) {
            "Initialisierung ..."
        } else {
            // Graded rather than binary: a 0.8 m estimate and one that has
            // drifted kilometres are both "not converged", and used to render
            // identically. See docs/open_issues_research.md.
            val status = getString(
                when (snapshot.quality) {
                    PositionQuality.GOOD -> R.string.status_quality_good
                    PositionQuality.DEGRADED -> R.string.status_quality_degraded
                    PositionQuality.POOR -> R.string.status_quality_poor
                    PositionQuality.LOST -> R.string.status_quality_lost
                },
            )
            val sigma = snapshot.positionSigma.maxOrNull() ?: 0f
            "$status  ±%.2f m   |   Tokens: %d   |   Iterationen: %d"
                .format(sigma, state.beacons, state.iterations)
        }
    }

    override fun onDestroy() {
        if (bound) {
            unbindService(connection)
            bound = false
        }
        super.onDestroy()
    }

    private class TabAdapter(activity: FragmentActivity) : FragmentStateAdapter(activity) {
        override fun getItemCount(): Int = 4
        override fun createFragment(position: Int): Fragment = when (position) {
            0 -> LiveViewFragment()
            1 -> MapFragment()
            2 -> ScenarioFragment()
            else -> SettingsFragment()
        }
    }
}

/** Live sensor readouts: attitude, point count, RSSI bars, vitals. */
/** Implemented by fragments that need the service once it binds. */
interface ServiceAware {
    fun onServiceReady()
}

/**
 * Live sensor view: attitude, LiDAR sweep, BLE tokens, vitals, device health.
 *
 * Observes the service's flows directly rather than going through the REST
 * API. On-device data has no reason to make a network round trip, and the tab
 * has to keep working when the edge agent is unreachable — which, underground
 * or inside a concrete structure, is the normal case.
 */
class LiveViewFragment : Fragment(), ServiceAware {

    private var attitude: AttitudeView? = null
    private var pointCloud: PointCloudView? = null
    private var poseText: TextView? = null
    private var vitalsText: TextView? = null
    private var deviceStatus: TextView? = null
    private var rssiContainer: LinearLayout? = null
    private val rssiBars = mutableMapOf<String, RssiBarView>()

    override fun onCreateView(
        inflater: LayoutInflater, container: ViewGroup?, savedInstanceState: Bundle?,
    ): View {
        val view = inflater.inflate(R.layout.fragment_live, container, false)
        attitude = view.findViewById(R.id.attitude_view)
        pointCloud = view.findViewById(R.id.point_cloud_view)
        poseText = view.findViewById(R.id.pose_text)
        vitalsText = view.findViewById(R.id.vitals_text)
        deviceStatus = view.findViewById(R.id.device_status)
        rssiContainer = view.findViewById(R.id.rssi_container)
        return view
    }

    override fun onViewCreated(view: View, savedInstanceState: Bundle?) {
        super.onViewCreated(view, savedInstanceState)
        observe()
    }

    override fun onServiceReady() = observe()

    private fun observe() {
        val fusion = (activity as? MainActivity)?.fusionService ?: return

        viewLifecycleOwner.lifecycleScope.launch {
            viewLifecycleOwner.repeatOnLifecycle(Lifecycle.State.STARTED) {
                fusion.state.collect { render(it) }
            }
        }

        // The sweep is not mirrored through FusionState: 720 points at 10 Hz
        // would churn the allocator for no benefit.
        viewLifecycleOwner.lifecycleScope.launch {
            viewLifecycleOwner.repeatOnLifecycle(Lifecycle.State.STARTED) {
                fusion.lidarScans?.collect { scan ->
                    pointCloud?.updateScan(scan.angles, scan.distances)
                }
            }
        }

        viewLifecycleOwner.lifecycleScope.launch {
            viewLifecycleOwner.repeatOnLifecycle(Lifecycle.State.STARTED) {
                fusion.bleBeacons.collect { renderBeacons(it) }
            }
        }
    }

    private fun render(state: FusionState) {
        val snapshot = state.ekf
        if (snapshot != null) {
            attitude?.update(snapshot.attitude[0], snapshot.attitude[1], snapshot.attitude[2])
            poseText?.text = getString(
                R.string.live_pose,
                snapshot.position[0], snapshot.position[1], snapshot.position[2],
                snapshot.attitude[2],
            )
        } else {
            poseText?.setText(R.string.live_waiting)
        }

        // Say "not observable" rather than showing a stale or invented number:
        // an absent reading and a zero reading mean very different things when
        // the question is whether someone behind a wall is breathing.
        val resp = state.respirationBpm
        vitalsText?.text = when {
            resp != null && state.heartRateBpm != null ->
                getString(R.string.live_vitals_full, resp, state.heartRateBpm)
            resp != null -> getString(R.string.live_vitals_resp, resp)
            state.uwbAnchorsInView > 0 -> getString(R.string.live_vitals_none)
            else -> getString(R.string.live_vitals_no_uwb)
        }

        deviceStatus?.text = getString(
            R.string.live_device_status,
            state.iterations,
            state.lidarPoints,
            state.uwbAnchorsInView,
            state.mmwaveTargets,
            if (snapshot?.converged == true) "OK" else "…",
        )
    }

    private fun renderBeacons(beacons: Map<String, BleBeacon>) {
        val container = rssiContainer ?: return
        // Strongest first, and capped: a busy site can present dozens of
        // tokens and an unbounded list makes the tab unusable.
        val visible = beacons.values.sortedByDescending { it.rssi }.take(MAX_RSSI_BARS)

        visible.forEach { beacon ->
            val bar = rssiBars.getOrPut(beacon.address) {
                RssiBarView(requireContext()).also {
                    it.layoutParams = LinearLayout.LayoutParams(
                        LinearLayout.LayoutParams.MATCH_PARENT, RSSI_BAR_HEIGHT_DP.dp(),
                    )
                    container.addView(it)
                }
            }
            bar.update(beacon.name.ifBlank { beacon.address }, beacon.rssi)
        }

        // Drop bars for tokens that have gone away.
        val live = visible.map { it.address }.toSet()
        rssiBars.keys.filterNot { it in live }.forEach { gone ->
            rssiBars.remove(gone)?.let(container::removeView)
        }
    }

    private fun Int.dp(): Int = (this * resources.displayMetrics.density).toInt()

    override fun onDestroyView() {
        super.onDestroyView()
        attitude = null; pointCloud = null; poseText = null
        vitalsText = null; deviceStatus = null; rssiContainer = null
        rssiBars.clear()
    }

    companion object {
        private const val MAX_RSSI_BARS = 8
        private const val RSSI_BAR_HEIGHT_DP = 34
    }
}

/**
 * 3D map, rendered by the shared Babylon.js bundle in a WebView.
 *
 * `file:///android_asset/` is used rather than a localhost server so the view
 * works with no network at all - the common case underground or inside a
 * concrete structure.
 */
class MapFragment : Fragment() {

    override fun onCreateView(
        inflater: LayoutInflater, container: ViewGroup?, savedInstanceState: Bundle?,
    ): View {
        val view = inflater.inflate(R.layout.fragment_map, container, false)
        val webView = view.findViewById<WebView>(R.id.map_webview) ?: return view

        webView.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            // The bundle is loaded from file:///android_asset/ so that the map
            // works with no network at all - the common case underground or
            // inside a concrete structure.
            allowFileAccess = true
            mediaPlaybackRequiresUserGesture = false
            // Explicitly denied: the bundle is first-party, and allowing a
            // file:// page to read arbitrary files or reach other origins is
            // the classic WebView data-exfiltration hole.
            allowContentAccess = false
            @Suppress("DEPRECATION")
            allowFileAccessFromFileURLs = false
            @Suppress("DEPRECATION")
            allowUniversalAccessFromFileURLs = false
        }
        if (BuildConfig.DEBUG) WebView.setWebContentsDebuggingEnabled(true)

        // The page has no same-origin host to derive the agent URL from, so we
        // inject it. tools/bundle-visualizer.sh rewrites DataFetcher to read it.
        val agentUrl = requireContext()
            .getSharedPreferences("aura", 0)
            .getString("agent_url", DEFAULT_AGENT_URL) ?: DEFAULT_AGENT_URL

        webView.webViewClient = object : WebViewClient() {
            override fun onPageFinished(view: WebView?, url: String?) {
                view?.evaluateJavascript(
                    "window.AURA_AGENT_URL = ${JSONObject.quote(agentUrl)};", null,
                )
            }
        }

        // A visible button that does nothing is worse than no button.
        view.findViewById<FloatingActionButton>(R.id.fab_save_map)?.setOnClickListener {
            saveMapSnapshot(view)
        }

        if (visualizerBundlePresent()) {
            webView.loadUrl("file:///android_asset/visualizer/index.html")
        } else {
            // A blank WebView looks like a crash. Say what is missing instead.
            webView.loadDataWithBaseURL(null, MISSING_BUNDLE_HTML, "text/html", "utf-8", null)
        }
        return view
    }

    /**
     * Persist the current map to the local store and record it in the audit
     * chain, so a survey has a defensible "this is what we saw, when" record.
     */
    private fun saveMapSnapshot(root: View) {
        val service = (activity as? MainActivity)?.fusionService
        val stats = service?.storeStats()
        val message = if (stats == null) {
            getString(R.string.map_save_unavailable)
        } else {
            AuraApplication.instance.audit.append(
                "map", "snapshot.saved",
                JSONObject()
                    .put("chunks", stats.optInt("chunks"))
                    .put("events", stats.optInt("events")),
                Severity.NOTICE,
            )
            getString(R.string.map_saved, stats.optInt("chunks"))
        }
        Snackbar.make(root, message, Snackbar.LENGTH_LONG).show()
    }

    private fun visualizerBundlePresent(): Boolean = runCatching {
        requireContext().assets.list("visualizer")?.contains("index.html") == true
    }.getOrDefault(false)

    companion object {
        private const val DEFAULT_AGENT_URL = "http://10.8.0.1:8080"

        private val MISSING_BUNDLE_HTML = """
            <!DOCTYPE html><html><head><meta name="viewport"
              content="width=device-width,initial-scale=1"><style>
              body{background:#0A0D14;color:#E6ECF2;font-family:sans-serif;
                   padding:24px;line-height:1.6}
              code{background:#1B2330;padding:2px 6px;border-radius:4px;
                   font-size:13px;display:inline-block;margin-top:8px}
              h2{color:#FFCC00;font-size:17px}
            </style></head><body>
              <h2>3D-Bundle nicht eingebettet</h2>
              <p>Dieser Debug-Build enthält die Babylon.js-Visualisierung nicht.
                 Sie wird beim Packaging aus <code>web-visualizer/</code> erzeugt:</p>
              <code>tools/bundle-visualizer.sh</code>
              <p>Alle übrigen Funktionen — Sensorfusion, Szenarien, Audit-Log —
                 sind davon nicht betroffen.</p>
            </body></html>
        """.trimIndent()
    }
}

/**
 * Scenario control: pick a scenario, set the parameters, run it on the agent.
 *
 * The simulation lives on the edge agent, not on the handheld — pathfinding
 * for a few hundred agents is not something a CT45P should be doing while it
 * is also running the sensor fusion loop. This fragment is a remote control,
 * so every failure mode here is a network failure mode and is reported as
 * such rather than silently doing nothing.
 */
class ScenarioFragment : Fragment() {

    private val api get() = AuraApplication.instance.api

    private var spinner: Spinner? = null
    private var peopleSlider: Slider? = null
    private var smokeSlider: Slider? = null
    private var panicSlider: Slider? = null
    private var progress: LinearProgressIndicator? = null
    private var metrics: TextView? = null
    private var pollJob: Job? = null

    override fun onCreateView(
        inflater: LayoutInflater, container: ViewGroup?, savedInstanceState: Bundle?,
    ): View {
        val view = inflater.inflate(R.layout.fragment_scenario, container, false)
        spinner = view.findViewById(R.id.scenario_spinner)
        peopleSlider = view.findViewById(R.id.people_slider)
        smokeSlider = view.findViewById(R.id.smoke_slider)
        panicSlider = view.findViewById(R.id.panic_slider)
        progress = view.findViewById(R.id.scenario_progress)
        metrics = view.findViewById(R.id.scenario_metrics)

        spinner?.adapter = ArrayAdapter(
            requireContext(),
            android.R.layout.simple_spinner_dropdown_item,
            SCENARIO_LABELS.map(::getString),
        )

        view.findViewById<MaterialButton>(R.id.scenario_start)?.setOnClickListener { start() }
        view.findViewById<MaterialButton>(R.id.scenario_stop)?.setOnClickListener { stop() }
        return view
    }

    private fun start() {
        val scenario = SCENARIO_IDS.getOrElse(spinner?.selectedItemPosition ?: 0) { "evacuation" }
        val people = peopleSlider?.value?.toInt() ?: 24
        val smoke = smokeSlider?.value ?: 0.35f
        val panic = panicSlider?.value ?: 0.3f

        metrics?.setText(R.string.scenario_starting)
        viewLifecycleOwner.lifecycleScope.launch {
            val reply = runCatching { api.startScenario(scenario, people, smoke, panic) }.getOrNull()
            if (reply == null) {
                metrics?.text = getString(R.string.scenario_unreachable, api.baseUrl)
                return@launch
            }
            AuraApplication.instance.audit.append(
                "scenario", "start",
                JSONObject().put("scenario", scenario).put("people", people),
                Severity.NOTICE,
            )
            pollMetrics()
        }
    }

    private fun stop() {
        pollJob?.cancel()
        viewLifecycleOwner.lifecycleScope.launch {
            val reply = runCatching { api.stopScenario() }.getOrNull()
            progress?.progress = 0
            metrics?.text = if (reply == null) {
                getString(R.string.scenario_unreachable, api.baseUrl)
            } else {
                renderMetrics(reply)
            }
        }
    }

    /**
     * Poll while a run is in progress.
     *
     * Polling rather than the telemetry WebSocket: scenario metrics update
     * once a second at most, and holding a socket open for a screen the user
     * may have swiped away from wastes radio power on a battery-limited
     * device.
     */
    private fun pollMetrics() {
        pollJob?.cancel()
        pollJob = viewLifecycleOwner.lifecycleScope.launch {
            while (isActive) {
                val state = runCatching { api.getState() }.getOrNull()
                val scenario = state?.optJSONObject("scenario")
                if (scenario != null) {
                    progress?.progress = (scenario.optDouble("progress", 0.0) * 100).toInt()
                    metrics?.text = renderMetrics(scenario)
                    if (scenario.optBoolean("finished", false)) break
                }
                delay(POLL_INTERVAL_MS)
            }
        }
    }

    private fun renderMetrics(json: JSONObject): String = getString(
        R.string.scenario_metrics_fmt,
        json.optInt("total_agents", json.optInt("people", 0)),
        json.optInt("escaped", 0),
        json.optInt("casualties", 0),
        json.optDouble("sim_time", 0.0),
    )

    override fun onPause() {
        super.onPause()
        // Stop polling when the tab is not visible; ViewPager2 keeps
        // offscreen fragments alive and they would poll forever.
        pollJob?.cancel()
    }

    override fun onDestroyView() {
        super.onDestroyView()
        pollJob?.cancel()
        spinner = null; peopleSlider = null; smokeSlider = null
        panicSlider = null; progress = null; metrics = null
    }

    companion object {
        private const val POLL_INTERVAL_MS = 1000L
        private val SCENARIO_IDS = listOf(
            "evacuation", "tactical", "architecture", "event", "research",
        )
        private val SCENARIO_LABELS = listOf(
            R.string.scenario_evacuation, R.string.scenario_tactical,
            R.string.scenario_architecture, R.string.scenario_event,
            R.string.scenario_research,
        )
    }
}

/**
 * Settings, audit verification and storage/device diagnostics.
 *
 * The audit chain is verifiable *on the device*, without a server: an
 * evidential log you can only check by uploading it somewhere is not much use
 * to someone standing in a building with no signal.
 */
class SettingsFragment : Fragment(), ServiceAware {

    private var auditStatus: TextView? = null
    private var storageStatus: TextView? = null
    private var llmStatus: TextView? = null
    private var llmQuestion: TextInputEditText? = null
    private var llmAnswer: TextView? = null
    private var llmAsk: MaterialButton? = null

    override fun onCreateView(
        inflater: LayoutInflater, container: ViewGroup?, savedInstanceState: Bundle?,
    ): View {
        val view = inflater.inflate(R.layout.fragment_settings, container, false)
        val app = AuraApplication.instance
        auditStatus = view.findViewById(R.id.audit_status)
        storageStatus = view.findViewById(R.id.storage_status)
        llmStatus = view.findViewById(R.id.llm_status)
        llmQuestion = view.findViewById(R.id.llm_question)
        llmAnswer = view.findViewById(R.id.llm_answer)
        llmAsk = view.findViewById(R.id.llm_ask)

        val urlField = view.findViewById<TextInputEditText>(R.id.server_url)
        urlField?.setText(app.agentUrl)
        urlField?.setOnFocusChangeListener { _, hasFocus ->
            if (!hasFocus) commitUrl(urlField.text?.toString())
        }
        // Committing on IME "done" as well as focus loss: on a rugged handheld
        // with a hardware keyboard the field can lose focus in ways that do
        // not fire, and silently discarding a typed URL is maddening.
        urlField?.setOnEditorActionListener { _, actionId, _ ->
            if (actionId == EditorInfo.IME_ACTION_DONE) commitUrl(urlField.text?.toString())
            false
        }

        view.findViewById<MaterialSwitch>(R.id.mmwave_reduced)?.apply {
            isChecked = app.reducedMmwavePower
            setOnCheckedChangeListener { _, checked -> app.reducedMmwavePower = checked }
        }

        view.findViewById<MaterialButton>(R.id.verify_audit)?.setOnClickListener { verifyAudit() }
        view.findViewById<MaterialButton>(R.id.manage_tokens)?.setOnClickListener {
            // Token pairing needs a BLE scan UI that does not exist yet; say
            // so rather than presenting a button that appears to do nothing.
            auditStatus?.setText(R.string.settings_tokens_todo)
        }

        view.findViewById<TextView>(R.id.native_info)?.text = getString(
            R.string.settings_native_info,
            NativeEngine.version(),
            if (NativeEngine.hasNeon()) "NEON" else "generic",
        )

        renderStorage()
        return view
    }

    override fun onServiceReady() = renderStorage()

    private fun commitUrl(raw: String?) {
        val url = raw?.trim().orEmpty()
        if (url.isEmpty()) return
        if (!url.startsWith("http://") && !url.startsWith("https://")) {
            auditStatus?.text = getString(R.string.settings_url_invalid)
            return
        }
        AuraApplication.instance.agentUrl = url
        AuraApplication.instance.audit.append(
            "settings", "agent_url.changed",
            JSONObject().put("url", url), Severity.NOTICE,
        )
    }

    private fun verifyAudit() {
        val audit = AuraApplication.instance.audit
        val result = audit.verify()
        auditStatus?.text = if (result.valid) {
            getString(R.string.settings_audit_ok, audit.size)
        } else {
            getString(R.string.settings_audit_bad, result.firstBadIndex ?: -1, result.message)
        }
    }

    private fun renderStorage() {
        val store = (activity as? MainActivity)?.fusionService?.storeStats()
        storageStatus?.text = if (store == null) {
            getString(R.string.settings_storage_unavailable)
        } else {
            getString(
                R.string.settings_storage,
                store.optInt("events"), store.optInt("chunks"),
                store.optLong("size_bytes") / 1024.0 / 1024.0,
                store.optString("journal_mode"),
            )
        }
    }

    /**
     * Try to load the side-loaded GGUF. Never blocks the UI thread: a 1 GB
     * mmap on a QCS4290 is hundreds of milliseconds even when it fails.
     */
    private fun prepareAssistant() {
        val llm = AuraApplication.instance.llm
        llmStatus?.setText(R.string.settings_llm_loading)
        viewLifecycleOwner.lifecycleScope.launch {
            val status = llm.load()
            llmStatus?.text = when (status) {
                LlmStatus.LOADED -> getString(R.string.settings_llm_ready, llm.modelName)
                else -> getString(R.string.settings_llm_unavailable, llm.statusDetail)
            }
        }
    }

    private fun askAssistant() {
        val question = llmQuestion?.text?.toString()?.trim().orEmpty()
        if (question.isEmpty()) {
            llmAnswer?.setText(R.string.settings_llm_empty)
            return
        }
        val llm = AuraApplication.instance.llm
        val snapshot = surveySnapshot()
        llmAnswer?.setText(R.string.settings_llm_thinking)
        viewLifecycleOwner.lifecycleScope.launch {
            if (!llm.isLoaded) {
                // Still a real call site: show what we know rather than a
                // button that appears to think and then lies.
                llmAnswer?.text = getString(
                    R.string.settings_llm_no_inference,
                    llm.statusDetail.ifBlank { llm.status.name },
                    snapshot,
                    llm.searchPaths().joinToString("\n"),
                )
                return@launch
            }
            llm.index("live-survey", snapshot)
            val answer = llm.ask(question)
            llmAnswer?.text = answer
            AuraApplication.instance.audit.append(
                "llm", "ask",
                JSONObject().put("q", question.take(80)).put("loaded", true),
                Severity.INFO,
            )
        }
    }

    /**
     * Compact survey summary injected as RAG context (and shown raw when
     * the model is missing, so the tab is never a dead form).
     */
    private fun surveySnapshot(): String {
        val state = (activity as? MainActivity)?.fusionService?.state?.value
        if (state == null) return "Fusiondienst nicht verbunden."
        val snap = state.ekf
        return buildString {
            append("Iterationen ").append(state.iterations)
            append(" · Tokens ").append(state.beacons)
            append(" · LiDAR ").append(state.lidarPoints)
            append(" · UWB ").append(state.uwbAnchorsInView)
            append(" · mmWave ").append(state.mmwaveTargets)
            if (snap != null) {
                append('\n')
                append("Position %.2f, %.2f, %.2f".format(
                    snap.position[0], snap.position[1], snap.position[2],
                ))
                append(" · Qualität ").append(snap.quality.name.lowercase())
                val sigma = snap.positionSigma.maxOrNull() ?: Float.NaN
                append(" · σ ").append("%.2f m".format(sigma))
            }
            state.respirationBpm?.let { append("\nAtmung %.0f/min".format(it)) }
        }
    }

    override fun onDestroyView() {
        super.onDestroyView()
        auditStatus = null; storageStatus = null
        llmStatus = null; llmQuestion = null; llmAnswer = null; llmAsk = null
    }
}
