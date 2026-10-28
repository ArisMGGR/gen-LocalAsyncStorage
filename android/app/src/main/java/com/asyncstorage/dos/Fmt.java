package com.asyncstorage.dos;

import java.util.Locale;

/** Small human-readable formatters shared across the UI and the transfer service. */
public final class Fmt {
    private Fmt() {}

    public static String size(long n) {
        double s = n;
        String[] u = {"B", "KB", "MB", "GB", "TB"};
        for (int i = 0; i < u.length; i++) {
            if (s < 1024 || i == u.length - 1) {
                return i == 0 ? String.format(Locale.US, "%.0f %s", s, u[i])
                        : String.format(Locale.US, "%.1f %s", s, u[i]);
            }
            s /= 1024;
        }
        return n + " B";
    }

    public static String speed(double bytesPerSec) {
        double r = bytesPerSec;
        String[] u = {"B/s", "KB/s", "MB/s", "GB/s"};
        for (int i = 0; i < u.length; i++) {
            if (r < 1024 || i == u.length - 1) {
                return i == 0 ? String.format(Locale.US, "%.0f %s", r, u[i])
                        : String.format(Locale.US, "%.1f %s", r, u[i]);
            }
            r /= 1024;
        }
        return String.format(Locale.US, "%.1f GB/s", r);
    }
}
