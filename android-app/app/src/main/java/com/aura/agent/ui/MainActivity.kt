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
import com.aura.agent.fusion.SensorFusionService
import com.aura.agent.sensors.BleBeacon
import com.aura.agent.security.Severity
import com.google.android.material.tabs.TabLayout
import com.google.android.material.tabs.TabLayoutMediator
import org.json.JSONObject
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
            val status = if (snapshot.converged) getString(R.string.status_converged)
            else getString(R.string.status_diverged)
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

        if (visualizerBundlePresent()) {
            webView.loadUrl("file:///android_asset/visualizer/index.html")
        } else {
            // A blank WebView looks like a crash. Say what is missing instead.
            webView.loadDataWithBaseURL(null, MISSING_BUNDLE_HTML, "text/html", "utf-8", null)
        }
        return view
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

class ScenarioFragment : Fragment() {
    override fun onCreateView(
        inflater: LayoutInflater, container: ViewGroup?, savedInstanceState: Bundle?,
    ): View = inflater.inflate(R.layout.fragment_scenario, container, false)
}

class SettingsFragment : Fragment() {
    override fun onCreateView(
        inflater: LayoutInflater, container: ViewGroup?, savedInstanceState: Bundle?,
    ): View = inflater.inflate(R.layout.fragment_settings, container, false)
}
