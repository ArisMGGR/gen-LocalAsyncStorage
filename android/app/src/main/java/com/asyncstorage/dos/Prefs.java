package com.asyncstorage.dos;

import android.content.Context;
import android.content.SharedPreferences;

/** Stores the server URL and API key (the Android equivalent of config.toml). */
public class Prefs {
    private static final String FILE = "config";
    private static final String KEY_URL = "server_url";
    private static final String KEY_API = "api_key";
    private static final String KEY_TREE = "download_tree_uri";

    private final SharedPreferences sp;

    public Prefs(Context context) {
        sp = context.getSharedPreferences(FILE, Context.MODE_PRIVATE);
    }

    public String serverUrl() {
        // Trim a trailing slash so path concatenation stays clean.
        String url = sp.getString(KEY_URL, "").trim();
        while (url.endsWith("/")) {
            url = url.substring(0, url.length() - 1);
        }
        return url;
    }

    public String apiKey() {
        return sp.getString(KEY_API, "").trim();
    }

    public boolean isConfigured() {
        return !serverUrl().isEmpty() && !apiKey().isEmpty();
    }

    public void save(String url, String apiKey) {
        sp.edit().putString(KEY_URL, url).putString(KEY_API, apiKey).apply();
    }

    /** The SAF tree URI of the user-chosen download folder ("" if unset). */
    public String downloadTreeUri() {
        return sp.getString(KEY_TREE, "");
    }

    public void saveDownloadTreeUri(String uri) {
        sp.edit().putString(KEY_TREE, uri).apply();
    }
}
