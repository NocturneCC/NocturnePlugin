package com.nocturne;

import com.google.gson.Gson;
import java.awt.image.BufferedImage;
import java.io.ByteArrayInputStream;
import java.io.IOException;
import java.nio.channels.FileChannel;
import java.nio.charset.StandardCharsets;
import java.nio.file.DirectoryStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.nio.file.StandardOpenOption;
import java.nio.file.attribute.PosixFileAttributeView;
import java.nio.file.attribute.PosixFilePermission;
import java.util.Collections;
import java.util.EnumSet;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Set;
import javax.imageio.ImageIO;

/** Atomic bounded cache of public manifest/image data only. */
final class EmojiCacheStore
{
	static final int MAX_GENERATIONS = 2;
	private static final Set<PosixFilePermission> DIRECTORY_PERMISSIONS = EnumSet.of(
		PosixFilePermission.OWNER_READ, PosixFilePermission.OWNER_WRITE,
		PosixFilePermission.OWNER_EXECUTE);
	private static final Set<PosixFilePermission> FILE_PERMISSIONS = EnumSet.of(
		PosixFilePermission.OWNER_READ, PosixFilePermission.OWNER_WRITE);

	private final Path root;
	private final Gson gson;

	EmojiCacheStore(Path root, Gson gson)
	{
		this.root = root;
		this.gson = gson;
	}

	Loaded load()
	{
		try
		{
			Path pointer = root.resolve("current");
			if (!Files.isRegularFile(pointer) || Files.isSymbolicLink(pointer)
				|| Files.size(pointer) > 128) return null;
			String revision = Files.readString(pointer, StandardCharsets.US_ASCII).trim();
			if (!EmojiManifest.DIGEST.matcher(revision).matches()) return null;
			Path generation = safeGeneration(revision);
			Path manifestPath = generation.resolve("manifest.json");
			if (!Files.isRegularFile(manifestPath) || Files.isSymbolicLink(manifestPath)
				|| Files.size(manifestPath) > EmojiManifest.MAX_MANIFEST_BYTES) return null;
			EmojiManifest manifest = EmojiManifest.parse(Files.readAllBytes(manifestPath), gson);
			if (!revision.equals(manifest.revision)) return null;
			Map<String, EmojiAsset> assets = new LinkedHashMap<>();
			Map<String, byte[]> rawAssets = new LinkedHashMap<>();
			for (EmojiManifest.Entry entry : manifest.entries)
			{
				Path path = generation.resolve("assets").resolve(entry.digest + ".png");
				byte[] raw = readAsset(path, entry);
				BufferedImage image = decode(raw);
				assets.put(entry.name, new EmojiAsset(entry.name, entry.digest, image));
				rawAssets.put(entry.digest, raw);
			}
			String etag = null;
			Path etagPath = generation.resolve("etag.txt");
			if (Files.isRegularFile(etagPath) && !Files.isSymbolicLink(etagPath)
				&& Files.size(etagPath) <= 80)
			{
				String value = Files.readString(etagPath, StandardCharsets.US_ASCII).trim();
				if (value.matches("\"[0-9a-f]{64}\"")) etag = value;
			}
			return new Loaded(manifest, assets, rawAssets, etag);
		}
		catch (IOException | RuntimeException error)
		{
			return null;
		}
	}

	void save(EmojiManifest manifest, Map<String, byte[]> rawAssets, String etag) throws IOException
	{
		if (rawAssets.size() > manifest.entries.size() || !etag.matches("\"[0-9a-f]{64}\""))
			throw new IOException("incomplete emoji generation");
		prepareDirectory(root);
		Path generations = root.resolve("generations");
		prepareDirectory(generations);
		Path target = safeGeneration(manifest.revision);
		if (Files.exists(target) && !generationValid(target, manifest))
		{
			deleteTree(target);
		}
		if (!Files.exists(target))
		{
			Path temporary = Files.createTempDirectory(root, ".generation-");
			setDirectoryPermissions(temporary);
			try
			{
				Path assets = temporary.resolve("assets");
				prepareDirectory(assets);
				for (EmojiManifest.Entry entry : manifest.entries)
				{
					byte[] raw = rawAssets.get(entry.digest);
					if (raw == null) throw new IOException("missing emoji asset");
					validateAsset(raw, entry);
					writeNew(assets.resolve(entry.digest + ".png"), raw);
				}
				writeNew(temporary.resolve("manifest.json"), manifest.raw);
				writeNew(temporary.resolve("etag.txt"), etag.getBytes(StandardCharsets.US_ASCII));
				forceDirectory(assets);
				forceDirectory(temporary);
				Files.move(temporary, target, StandardCopyOption.ATOMIC_MOVE);
				forceDirectory(generations);
			}
			finally { deleteTree(temporary); }
		}
		Path pointer = Files.createTempFile(root, ".current-", ".tmp");
		try
		{
			Files.writeString(pointer, manifest.revision + "\n", StandardCharsets.US_ASCII,
				StandardOpenOption.TRUNCATE_EXISTING);
			setFilePermissions(pointer);
			forceFile(pointer);
			Files.move(pointer, root.resolve("current"), StandardCopyOption.ATOMIC_MOVE,
				StandardCopyOption.REPLACE_EXISTING);
			forceDirectory(root);
		}
		finally { Files.deleteIfExists(pointer); }
		cleanup(manifest.revision);
	}

	private boolean generationValid(Path generation, EmojiManifest expected)
	{
		try
		{
			if (!Files.isDirectory(generation) || Files.isSymbolicLink(generation)) return false;
			Path manifestPath = generation.resolve("manifest.json");
			if (!Files.isRegularFile(manifestPath) || Files.isSymbolicLink(manifestPath)) return false;
			EmojiManifest actual = EmojiManifest.parse(Files.readAllBytes(manifestPath), gson);
			if (!actual.revision.equals(expected.revision)) return false;
			for (EmojiManifest.Entry entry : actual.entries)
				readAsset(generation.resolve("assets").resolve(entry.digest + ".png"), entry);
			return true;
		}
		catch (IOException | RuntimeException error)
		{
			return false;
		}
	}

	private byte[] readAsset(Path path, EmojiManifest.Entry entry) throws IOException
	{
		if (!Files.isRegularFile(path) || Files.isSymbolicLink(path)
			|| Files.size(path) != entry.byteLength) throw new IOException("invalid cached asset");
		byte[] raw = Files.readAllBytes(path);
		validateAsset(raw, entry);
		return raw;
	}

	static BufferedImage validateAsset(byte[] raw, EmojiManifest.Entry entry) throws IOException
	{
		if (raw == null || raw.length != entry.byteLength || raw.length > EmojiManifest.MAX_ASSET_BYTES
			|| raw.length < 33 || raw[0] != (byte) 0x89 || raw[1] != 'P' || raw[2] != 'N' || raw[3] != 'G'
			|| readInt(raw, 16) != entry.width || readInt(raw, 20) != entry.height
			|| raw[raw.length - 12] != 0 || raw[raw.length - 11] != 0
			|| raw[raw.length - 10] != 0 || raw[raw.length - 9] != 0
			|| raw[raw.length - 8] != 'I' || raw[raw.length - 7] != 'E'
			|| raw[raw.length - 6] != 'N' || raw[raw.length - 5] != 'D'
			|| !EmojiManifest.sha256(raw).equals(entry.digest))
			throw new IOException("invalid emoji asset bytes");
		BufferedImage image = decode(raw);
		if (image.getWidth() != entry.width || image.getHeight() != entry.height
			|| !image.getColorModel().hasAlpha()) throw new IOException("invalid emoji image");
		return image;
	}

	private static int readInt(byte[] raw, int offset)
	{
		return (raw[offset] & 0xff) << 24 | (raw[offset + 1] & 0xff) << 16
			| (raw[offset + 2] & 0xff) << 8 | raw[offset + 3] & 0xff;
	}

	private static BufferedImage decode(byte[] raw) throws IOException
	{
		synchronized (ImageIO.class)
		{
			BufferedImage image = ImageIO.read(new ByteArrayInputStream(raw));
			if (image == null) throw new IOException("undecodable emoji image");
			return image;
		}
	}

	private Path safeGeneration(String revision) throws IOException
	{
		if (!EmojiManifest.DIGEST.matcher(revision).matches()) throw new IOException("unsafe revision");
		Path generations = root.resolve("generations").toAbsolutePath().normalize();
		Path target = generations.resolve(revision).normalize();
		if (!target.getParent().equals(generations)) throw new IOException("unsafe generation path");
		return target;
	}

	private void cleanup(String current) throws IOException
	{
		Path generations = root.resolve("generations");
		int retained = 1;
		try (DirectoryStream<Path> stream = Files.newDirectoryStream(generations))
		{
			for (Path path : stream)
			{
				if (!Files.isDirectory(path) || Files.isSymbolicLink(path)
					|| !EmojiManifest.DIGEST.matcher(path.getFileName().toString()).matches()
					|| path.getFileName().toString().equals(current)) continue;
				if (retained++ < MAX_GENERATIONS) continue;
				deleteTree(path);
			}
		}
	}

	private static void deleteTree(Path path) throws IOException
	{
		if (!Files.exists(path) || Files.isSymbolicLink(path))
		{
			Files.deleteIfExists(path);
			return;
		}
		if (Files.isDirectory(path))
		{
			try (DirectoryStream<Path> stream = Files.newDirectoryStream(path))
			{
				for (Path child : stream) deleteTree(child);
			}
		}
		Files.deleteIfExists(path);
	}

	private static void prepareDirectory(Path path) throws IOException
	{
		Files.createDirectories(path);
		if (!Files.isDirectory(path) || Files.isSymbolicLink(path))
			throw new IOException("unsafe emoji cache directory");
		setDirectoryPermissions(path);
	}

	private static void writeNew(Path path, byte[] raw) throws IOException
	{
		Files.write(path, raw, StandardOpenOption.CREATE_NEW, StandardOpenOption.WRITE);
		setFilePermissions(path);
		forceFile(path);
	}

	private static boolean posix(Path path) throws IOException
	{
		return Files.getFileStore(path).supportsFileAttributeView(PosixFileAttributeView.class);
	}

	private static void setDirectoryPermissions(Path path) throws IOException
	{
		if (posix(path)) Files.setPosixFilePermissions(path, DIRECTORY_PERMISSIONS);
	}

	private static void setFilePermissions(Path path) throws IOException
	{
		if (posix(path)) Files.setPosixFilePermissions(path, FILE_PERMISSIONS);
	}

	private static void forceFile(Path path) throws IOException
	{
		try (FileChannel channel = FileChannel.open(path, StandardOpenOption.WRITE)) { channel.force(true); }
	}

	private static void forceDirectory(Path path) throws IOException
	{
		try (FileChannel channel = FileChannel.open(path, StandardOpenOption.READ)) { channel.force(true); }
	}

	static final class Loaded
	{
		final EmojiManifest manifest;
		final Map<String, EmojiAsset> assets;
		final Map<String, byte[]> rawAssets;
		final String etag;

		Loaded(EmojiManifest manifest, Map<String, EmojiAsset> assets,
			Map<String, byte[]> rawAssets, String etag)
		{
			this.manifest = manifest;
			this.assets = Collections.unmodifiableMap(new LinkedHashMap<>(assets));
			this.rawAssets = Collections.unmodifiableMap(new LinkedHashMap<>(rawAssets));
			this.etag = etag;
		}
	}
}
