using System.IO;
using System.Text.Json;
using ETuner.Models;

namespace ETuner.Services;

public static class BackupService
{
    public static string CreateBackup(List<(string path, TagInfo original)> files)
    {
        var records = files.Select(f => new
        {
            path = f.path,
            artist = f.original.Artist,
            albumArtist = f.original.AlbumArtist,
            albumArtistSort = f.original.AlbumArtistSort,
            title = f.original.Title,
            album = f.original.Album,
            track = f.original.Track,
            totalTracks = f.original.TotalTracks,
            year = f.original.Year,
            genre = f.original.Genre,
        }).ToList();

        var json = JsonSerializer.Serialize(new { timestamp = DateTime.Now, records },
            new JsonSerializerOptions { WriteIndented = true });

        var dir = Path.GetDirectoryName(files[0].path) ?? Environment.CurrentDirectory;
        var backupPath = Path.Combine(dir, $"etuner_backup_{DateTime.Now:yyyyMMdd_HHmmss}.json");
        File.WriteAllText(backupPath, json, System.Text.Encoding.UTF8);
        return backupPath;
    }

    public static bool RestoreBackup(string backupPath, bool dryRun = true)
    {
        if (!File.Exists(backupPath)) return false;

        var json = File.ReadAllText(backupPath, System.Text.Encoding.UTF8);
        var doc = JsonDocument.Parse(json);
        var root = doc.RootElement;

        if (!root.TryGetProperty("records", out var records)) return false;

        foreach (var record in records.EnumerateArray())
        {
            var path = record.GetProperty("path").GetString();
            if (string.IsNullOrEmpty(path) || !File.Exists(path)) continue;

var tags = new TagInfo();
            if (record.TryGetProperty("artist", out var p1)) tags.Artist = p1.GetString()?.Trim();
            if (record.TryGetProperty("albumArtist", out var p2)) tags.AlbumArtist = p2.GetString()?.Trim();
            if (record.TryGetProperty("title", out var p3)) tags.Title = p3.GetString()?.Trim();
            if (record.TryGetProperty("album", out var p4)) tags.Album = p4.GetString()?.Trim();
            if (record.TryGetProperty("year", out var p5) && p5.TryGetInt32(out var y)) tags.Year = y;
            if (record.TryGetProperty("track", out var p6) && p6.TryGetInt32(out var t)) tags.Track = t;

            if (!dryRun) MetadataWriter.WriteTags(path, tags);
        }

        return true;
    }
}
