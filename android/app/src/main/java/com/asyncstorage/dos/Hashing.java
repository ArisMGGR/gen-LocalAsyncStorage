package com.asyncstorage.dos;

import java.io.InputStream;
import java.security.MessageDigest;

/** sha256 helpers matching the server's hex digest format. */
public final class Hashing {
    private Hashing() {}

    public static String hex(byte[] digest) {
        StringBuilder sb = new StringBuilder(digest.length * 2);
        for (byte b : digest) {
            sb.append(Character.forDigit((b >> 4) & 0xF, 16));
            sb.append(Character.forDigit(b & 0xF, 16));
        }
        return sb.toString();
    }

    /** sha256 of a stream, read in chunks. Closes nothing; caller owns the stream. */
    public static String sha256(InputStream in) throws Exception {
        MessageDigest md = MessageDigest.getInstance("SHA-256");
        byte[] buf = new byte[1 << 16];
        int n;
        while ((n = in.read(buf)) >= 0) {
            md.update(buf, 0, n);
        }
        return hex(md.digest());
    }
}
