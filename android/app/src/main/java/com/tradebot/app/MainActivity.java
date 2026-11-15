package com.tradebot.app;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.ContentValues;
import android.content.Intent;
import android.graphics.Bitmap;
import android.graphics.Color;
import android.hardware.biometrics.BiometricManager;
import android.hardware.biometrics.BiometricPrompt;
import android.net.Uri;
import android.net.http.SslError;
import android.os.Build;
import android.os.Bundle;
import android.os.CancellationSignal;
import android.os.SystemClock;
import android.provider.MediaStore;
import android.util.Base64;
import android.view.Gravity;
import android.view.View;
import android.view.WindowInsets;
import android.webkit.JavascriptInterface;
import android.webkit.JsResult;
import android.webkit.SslErrorHandler;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;

import java.io.OutputStream;

/**
 * The TradeBot app: opens your bot server's dashboard full-screen, behind an optional
 * fingerprint/PIN lock. All trading happens on the server; this is the remote control.
 */
public class MainActivity extends Activity {
    static final String EXTRA_RELOAD = "reload";
    private static final long RELOCK_AFTER_MS = 60_000;

    private String server;
    private WebView web;
    private LinearLayout errorView;
    private TextView errorText;
    private LinearLayout lockView;
    private boolean unlocked;
    private boolean prompting;
    private long backgroundedAt;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        server = Prefs.server(this);
        if (server == null) {
            startActivity(new Intent(this, SetupActivity.class));
            finish();
            return;
        }

        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(Color.parseColor("#0d1117"));

        web = new WebView(this);
        setUpWebView(web);
        root.addView(web, new FrameLayout.LayoutParams(-1, -1));

        errorView = panel();
        errorText = label("", 15, "#e6edf3");
        errorView.addView(errorText);
        errorView.addView(button("Try again", v -> { errorView.setVisibility(View.GONE); web.reload(); }));
        errorView.addView(button("Change server", v -> changeServer()));
        errorView.setVisibility(View.GONE);
        root.addView(errorView, new FrameLayout.LayoutParams(-1, -1));

        lockView = panel();
        lockView.addView(label("TradeBot is locked", 20, "#e6edf3"));
        lockView.addView(button("Unlock", v -> promptUnlock()));
        lockView.setVisibility(View.GONE);
        root.addView(lockView, new FrameLayout.LayoutParams(-1, -1));

        padForSystemBars(root);
        setContentView(root);

        if (state != null) web.restoreState(state);
        else web.loadUrl(server + "/");
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        if (intent.getBooleanExtra(EXTRA_RELOAD, false)) {
            server = Prefs.server(this);
            errorView.setVisibility(View.GONE);
            web.clearHistory();
            web.loadUrl(server + "/");
        }
    }

    @Override
    protected void onStart() {
        super.onStart();
        if (web == null) return;
        boolean stale = backgroundedAt != 0 && SystemClock.elapsedRealtime() - backgroundedAt > RELOCK_AFTER_MS;
        if (stale) unlocked = false;
        if (!unlocked && lockActive()) {
            lockView.setVisibility(View.VISIBLE);
            promptUnlock();
        } else {
            unlocked = true;
            lockView.setVisibility(View.GONE);
        }
        web.onResume();
    }

    @Override
    protected void onStop() {
        super.onStop();
        if (web == null) return;
        backgroundedAt = SystemClock.elapsedRealtime();
        web.onPause();
    }

    @Override
    protected void onSaveInstanceState(Bundle out) {
        super.onSaveInstanceState(out);
        if (web != null) web.saveState(out);
    }

    @Override
    protected void onDestroy() {
        if (web != null) web.destroy();
        super.onDestroy();
    }

    @Override
    @SuppressWarnings("deprecation")
    public void onBackPressed() {
        if (web != null && web.canGoBack()) web.goBack();
        else super.onBackPressed();
    }

    // ── web view ────────────────────────────────────────────────────────────

    @android.annotation.SuppressLint({"SetJavaScriptEnabled", "AddJavascriptInterface"})
    private void setUpWebView(WebView w) {
        WebSettings s = w.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);           // the dashboard keeps its sign-in in localStorage
        s.setAllowFileAccess(false);
        s.setAllowContentAccess(false);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        s.setSupportMultipleWindows(false);
        s.setMediaPlaybackRequiresUserGesture(true);
        w.setBackgroundColor(Color.parseColor("#0d1117"));
        w.addJavascriptInterface(new Bridge(), "TradeBotApp");

        w.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest req) {
                String url = req.getUrl().toString();
                if (Prefs.sameOrigin(server, url)) return false;
                // Anything else (news links, etc.) opens in the normal browser, never inside the app.
                try { startActivity(new Intent(Intent.ACTION_VIEW, req.getUrl())); } catch (Exception ignored) {}
                return true;
            }

            @Override
            public void onPageStarted(WebView view, String url, Bitmap favicon) {
                errorView.setVisibility(View.GONE);
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest req, WebResourceError err) {
                if (req.isForMainFrame()) {
                    showError("Can't reach your bot server.\n\n" + err.getDescription()
                            + "\n\nCheck your internet connection, and that the server is running.");
                }
            }

            @Override
            public void onReceivedSslError(WebView view, SslErrorHandler handler, SslError error) {
                handler.cancel();  // never accept a bad certificate on a trading app
                showError("The server's security certificate isn't valid, so the app refused to connect.");
            }
        });

        w.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onJsAlert(WebView view, String url, String message, JsResult result) {
                dialog().setMessage(message)
                        .setPositiveButton("OK", (d, x) -> result.confirm())
                        .setOnCancelListener(d -> result.confirm())
                        .show();
                return true;
            }

            @Override
            public boolean onJsConfirm(WebView view, String url, String message, JsResult result) {
                dialog().setMessage(message)
                        .setPositiveButton("OK", (d, x) -> result.confirm())
                        .setNegativeButton("Cancel", (d, x) -> result.cancel())
                        .setOnCancelListener(d -> result.cancel())
                        .show();
                return true;
            }
        });
    }

    private void showError(String msg) {
        errorText.setText(msg);
        errorView.setVisibility(View.VISIBLE);
    }

    private void changeServer() {
        startActivity(new Intent(this, SetupActivity.class));
    }

    // ── fingerprint / PIN lock ─────────────────────────────────────────────

    private boolean canAuthenticate() {
        BiometricManager bm = getSystemService(BiometricManager.class);
        if (bm == null) return false;
        if (Build.VERSION.SDK_INT >= 30) {
            return bm.canAuthenticate(BiometricManager.Authenticators.BIOMETRIC_WEAK
                    | BiometricManager.Authenticators.DEVICE_CREDENTIAL) == BiometricManager.BIOMETRIC_SUCCESS;
        }
        android.app.KeyguardManager km = getSystemService(android.app.KeyguardManager.class);
        return km != null && km.isDeviceSecure();
    }

    private boolean lockActive() {
        return Prefs.lockEnabled(this) && canAuthenticate();
    }

    @SuppressWarnings("deprecation")
    private void promptUnlock() {
        if (prompting) return;
        BiometricPrompt.Builder b = new BiometricPrompt.Builder(this)
                .setTitle("Unlock TradeBot")
                .setSubtitle("Use your fingerprint or screen lock");
        if (Build.VERSION.SDK_INT >= 30) {
            b.setAllowedAuthenticators(BiometricManager.Authenticators.BIOMETRIC_WEAK
                    | BiometricManager.Authenticators.DEVICE_CREDENTIAL);
        } else {
            b.setDeviceCredentialAllowed(true);
        }
        prompting = true;
        b.build().authenticate(new CancellationSignal(), getMainExecutor(),
                new BiometricPrompt.AuthenticationCallback() {
                    @Override
                    public void onAuthenticationSucceeded(BiometricPrompt.AuthenticationResult r) {
                        prompting = false;
                        unlocked = true;
                        lockView.setVisibility(View.GONE);
                    }

                    @Override
                    public void onAuthenticationError(int code, CharSequence msg) {
                        prompting = false;  // stays locked; the Unlock button tries again
                    }
                });
    }

    // ── bridge the dashboard can call as window.TradeBotApp ────────────────

    private final class Bridge {
        @JavascriptInterface
        public String server() {
            return server;
        }

        @JavascriptInterface
        public boolean lockEnabled() {
            return Prefs.lockEnabled(MainActivity.this);
        }

        @JavascriptInterface
        public void setLockEnabled(boolean on) {
            runOnUiThread(() -> {
                if (on && !canAuthenticate()) {
                    toast("Set up a fingerprint or screen lock in Android settings first.");
                    return;
                }
                Prefs.setLockEnabled(MainActivity.this, on);
                toast(on ? "App lock on" : "App lock off");
            });
        }

        @JavascriptInterface
        public void changeServer() {
            runOnUiThread(MainActivity.this::changeServer);
        }

        /** Saves a file the dashboard made (the tax CSV) to Downloads. */
        @JavascriptInterface
        public void saveFile(String name, String mime, String base64) {
            String safe = name == null ? "tradebot.csv" : name.replaceAll("[^A-Za-z0-9._-]", "_");
            try {
                byte[] data = Base64.decode(base64, Base64.DEFAULT);
                ContentValues v = new ContentValues();
                v.put(MediaStore.Downloads.DISPLAY_NAME, safe);
                v.put(MediaStore.Downloads.MIME_TYPE, mime == null ? "application/octet-stream" : mime);
                Uri uri = getContentResolver().insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, v);
                if (uri == null) throw new IllegalStateException("no Downloads folder");
                try (OutputStream out = getContentResolver().openOutputStream(uri)) {
                    if (out == null) throw new IllegalStateException("couldn't open the file");
                    out.write(data);
                }
                runOnUiThread(() -> toast("Saved " + safe + " to Downloads"));
            } catch (Exception e) {
                runOnUiThread(() -> toast("Couldn't save the file: " + e.getMessage()));
            }
        }
    }

    // ── small helpers ──────────────────────────────────────────────────────

    /** Keeps content clear of the status bar, navigation bar and keyboard (Android 15 draws edge to edge). */
    @SuppressWarnings("deprecation")
    static void padForSystemBars(View v) {
        v.setOnApplyWindowInsetsListener((view, insets) -> {
            int l, t, r, b;
            if (Build.VERSION.SDK_INT >= 30) {
                android.graphics.Insets i = insets.getInsets(WindowInsets.Type.systemBars() | WindowInsets.Type.ime());
                l = i.left; t = i.top; r = i.right; b = i.bottom;
            } else {
                l = insets.getSystemWindowInsetLeft(); t = insets.getSystemWindowInsetTop();
                r = insets.getSystemWindowInsetRight(); b = insets.getSystemWindowInsetBottom();
            }
            view.setPadding(l, t, r, b);
            return insets;
        });
    }

    private AlertDialog.Builder dialog() {
        return new AlertDialog.Builder(this, android.R.style.Theme_Material_Dialog_Alert);
    }

    private void toast(String s) {
        Toast.makeText(this, s, Toast.LENGTH_LONG).show();
    }

    private LinearLayout panel() {
        LinearLayout p = new LinearLayout(this);
        p.setOrientation(LinearLayout.VERTICAL);
        p.setGravity(Gravity.CENTER);
        int pad = dp(32);
        p.setPadding(pad, pad, pad, pad);
        p.setBackgroundColor(Color.parseColor("#0d1117"));
        p.setClickable(true);  // block taps reaching the dashboard underneath
        return p;
    }

    private TextView label(String s, int sp, String color) {
        TextView t = new TextView(this);
        t.setText(s);
        t.setTextSize(sp);
        t.setTextColor(Color.parseColor(color));
        t.setGravity(Gravity.CENTER);
        t.setPadding(0, 0, 0, dp(20));
        return t;
    }

    private Button button(String s, View.OnClickListener l) {
        Button b = new Button(this);
        b.setText(s);
        b.setOnClickListener(l);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(dp(220), LinearLayout.LayoutParams.WRAP_CONTENT);
        lp.topMargin = dp(8);
        b.setLayoutParams(lp);
        return b;
    }

    private int dp(int v) {
        return Math.round(v * getResources().getDisplayMetrics().density);
    }
}
