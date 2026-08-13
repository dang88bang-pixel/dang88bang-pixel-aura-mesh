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
import androidx.lifecycle.lifecycleScope
import androidx.lifecycle.repeatOnLifecycle
import androidx.viewpager2.adapter.FragmentStateAdapter
import androidx.viewpager2.widget.ViewPager2
import com.aura.agent.AuraApplication
import com.aura.agent.BuildConfig
import com.aura.agent.R
import com.aura.agent.fusion.FusionState
import com.aura.agent.fusion.SensorFusionService
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

    private val connection = object : ServiceConnection {
        override fun onServiceConnected(name: ComponentName?, binder: IBinder?) {
            service = (binder as? SensorFusionService.LocalBinder)?.service()
            bound = true
            observeFusion()
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
class LiveViewFragment : Fragment() {
    override fun onCreateView(
        inflater: LayoutInflater, container: ViewGroup?, savedInstanceState: Bundle?,
    ): View = inflater.inflate(R.layout.fragment_live, container, false)
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
