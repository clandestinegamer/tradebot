package com.tradebot.app;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.graphics.Color;
import android.os.Bundle;
import android.text.InputType;
import android.view.Gravity;
import android.view.inputmethod.EditorInfo;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;

/** First-run screen: enter the address of your TradeBot server and check that it answers. */
public class SetupActivity extends Activity {
    private EditText input;
    private TextView status;
    private Button connect;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        int pad = dp(24);

        LinearLayout col = new LinearLayout(this);
        col.setOrientation(LinearLayout.VERTICAL);
        col.setPadding(pad, pad * 2, pad, pad);

        TextView title = text("TradeBot", 28, Color.parseColor("#e6edf3"));
        col.addView(title);

        TextView body = text("Enter your bot server's address, the same one you set as DOMAIN in .env "
                + "(for example bot.yourdomain.com).\n\nThe bot itself runs on that server around the clock, "
                + "so trading keeps going when your phone is off. This app is your remote control.",
                15, Color.parseColor("#8b949e"));
        body.setPadding(0, dp(12), 0, dp(20));
        col.addView(body);

        input = new EditText(this);
        input.setHint("bot.yourdomain.com");
        input.setSingleLine(true);
        input.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        input.setImeOptions(EditorInfo.IME_ACTION_GO);
        input.setTextColor(Color.parseColor("#e6edf3"));
        input.setHintTextColor(Color.parseColor("#6e7681"));
        String saved = Prefs.server(this);
        if (saved != null) input.setText(saved);
        input.setOnEditorActionListener((v, id, ev) -> {
            if (id == EditorInfo.IME_ACTION_GO) { onConnect(); return true; }
            return false;
        });
        col.addView(input);

        connect = new Button(this);
        connect.setText("Connect");
        connect.setOnClickListener(v -> onConnect());
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT);
        lp.topMargin = dp(16);
        col.addView(connect, lp);

        status = text("", 14, Color.parseColor("#8b949e"));
        status.setPadding(0, dp(16), 0, 0);
        col.addView(status);

        TextView tip = text("Testing on home Wi-Fi first? Start the bot on your PC with --host 0.0.0.0 "
                + "and enter http://<PC address>:8000 here.", 13, Color.parseColor("#6e7681"));
        tip.setPadding(0, dp(28), 0, 0);
        col.addView(tip);

        ScrollView scroll = new ScrollView(this);
        scroll.setFillViewport(true);
        scroll.addView(col);
        MainActivity.padForSystemBars(scroll);
        setContentView(scroll);
    }

    private void onConnect() {
        String url = Prefs.normalize(input.getText().toString());
        if (url == null) {
            status.setTextColor(Color.parseColor("#f85149"));
            status.setText("That doesn't look like a web address.");
            return;
        }
        if (Prefs.insecureRemote(url)) {
            new AlertDialog.Builder(this, android.R.style.Theme_Material_Dialog_Alert)
                    .setTitle("Unencrypted connection")
                    .setMessage("This address uses plain http outside your home network, so your password and "
                            + "trades would travel unencrypted. Use https for a real server.")
                    .setPositiveButton("Use anyway", (d, w) -> check(url))
                    .setNegativeButton("Cancel", null)
                    .show();
            return;
        }
        check(url);
    }

    private void check(String url) {
        connect.setEnabled(false);
        status.setTextColor(Color.parseColor("#8b949e"));
        status.setText("Checking " + url + " …");
        new Thread(() -> {
            String problem = probe(url);
            runOnUiThread(() -> {
                connect.setEnabled(true);
                if (problem != null) {
                    status.setTextColor(Color.parseColor("#f85149"));
                    status.setText(problem);
                    return;
                }
                Prefs.setServer(this, url);
                Intent i = new Intent(this, MainActivity.class);
                i.addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP | Intent.FLAG_ACTIVITY_NEW_TASK);
                i.putExtra(MainActivity.EXTRA_RELOAD, true);
                startActivity(i);
                finish();
            });
        }).start();
    }

    /** Calls /healthz. Returns null when a TradeBot server answered, otherwise a message to show. */
    static String probe(String url) {
        HttpURLConnection c = null;
        try {
            c = (HttpURLConnection) new URL(url + "/healthz").openConnection();
            c.setConnectTimeout(8000);
            c.setReadTimeout(8000);
            int code = c.getResponseCode();
            InputStream in = code >= 400 ? c.getErrorStream() : c.getInputStream();
            String body = read(in);
            // 503 just means the bot hasn't finished a check yet; it's still a TradeBot server.
            if ((code == 200 || code == 503) && body.contains("seconds_since_check")) return null;
            return "Something answered at that address, but it isn't a TradeBot server (HTTP " + code + ").";
        } catch (javax.net.ssl.SSLException e) {
            return "Secure connection failed. Check the address, and that Caddy has issued the HTTPS certificate.";
        } catch (java.net.UnknownHostException e) {
            return "Couldn't find that address. Check the spelling, and that the domain points at your server.";
        } catch (Exception e) {
            return "Couldn't reach the server: " + e.getMessage();
        } finally {
            if (c != null) c.disconnect();
        }
    }

    private static String read(InputStream in) throws java.io.IOException {
        if (in == null) return "";
        try (InputStream s = in) {
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buf = new byte[4096];
            int n, total = 0;
            while ((n = s.read(buf)) > 0 && total < 65536) { out.write(buf, 0, n); total += n; }
            return new String(out.toByteArray(), StandardCharsets.UTF_8);
        }
    }

    private TextView text(String s, int sp, int color) {
        TextView t = new TextView(this);
        t.setText(s);
        t.setTextSize(sp);
        t.setTextColor(color);
        t.setGravity(Gravity.START);
        return t;
    }

    private int dp(int v) {
        return Math.round(v * getResources().getDisplayMetrics().density);
    }
}
