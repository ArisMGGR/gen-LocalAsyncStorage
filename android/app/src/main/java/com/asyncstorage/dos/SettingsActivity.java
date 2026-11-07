package com.asyncstorage.dos;

import android.os.Bundle;
import android.widget.Button;
import android.widget.EditText;
import android.widget.Toast;

import androidx.appcompat.app.AppCompatActivity;

/** Enter and save the server URL + API key (replaces config.toml). */
public class SettingsActivity extends AppCompatActivity {

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_settings);
        setTitle(R.string.settings_title);

        Prefs prefs = new Prefs(this);
        EditText urlField = findViewById(R.id.settings_url);
        EditText keyField = findViewById(R.id.settings_key);
        Button save = findViewById(R.id.settings_save);

        urlField.setText(prefs.serverUrl());
        keyField.setText(prefs.apiKey());

        save.setOnClickListener(v -> {
            String url = urlField.getText().toString().trim();
            String key = keyField.getText().toString().trim();
            if (url.isEmpty() || key.isEmpty()) {
                Toast.makeText(this, R.string.settings_incomplete, Toast.LENGTH_SHORT).show();
                return;
            }
            if (!isAscii(key)) {
                Toast.makeText(this, R.string.settings_bad_key, Toast.LENGTH_LONG).show();
                return;
            }
            String normalized = normalizeUrl(url);
            if (normalized == null) {
                Toast.makeText(this, R.string.settings_bad_url, Toast.LENGTH_LONG).show();
                return;
            }
            prefs.save(normalized, key);
            Toast.makeText(this, R.string.settings_saved, Toast.LENGTH_SHORT).show();
            finish();
        });
    }

    /** An HTTP header can't carry anything else, as the desktop's Settings says too. */
    static boolean isAscii(String text) {
        for (int i = 0; i < text.length(); i++) {
            if (text.charAt(i) > 127) return false;
        }
        return true;
    }

    /**
     * {@code text} as an http(s) server URL, or null if it isn't one.
     * "192.168.1.50:8000" gains its "http://", like the desktop app's Settings.
     */
    static String normalizeUrl(String text) {
        String url = text.trim();
        if (!url.matches("^[A-Za-z][A-Za-z0-9+.-]*://.*")) url = "http://" + url;
        try {
            java.net.URL u = new java.net.URL(url);
            String scheme = u.getProtocol().toLowerCase(java.util.Locale.ROOT);
            if (!(scheme.equals("http") || scheme.equals("https")) || u.getHost().isEmpty()) {
                return null;
            }
        } catch (java.net.MalformedURLException e) {
            return null;
        }
        while (url.endsWith("/")) url = url.substring(0, url.length() - 1);
        return url;
    }
}
