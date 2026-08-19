using System.Text.Json;
using ETuner.Models;
using TagLib;

namespace ETuner.Services;

public class TagFixOptions
{
    public bool DedupArtists { get; set; } = true;
    public bool DeriveAlbumArtist { get; set; } = true;
    public bool SetSortForm { get; set; } = true;
    public bool TrimWhitespace { get; set; } = true;
    public bool FixGenre { get; set; } = true;
    public bool FixTitleCase { get; set; } = false;
}

public static class MetadataWriter
{
    public static TagInfo ApplyFixes(string filePath, TagInfo original, TagFixOptions? options = null)
    {
        options ??= new TagFixOptions();

        var dedupedArtist = options.DedupArtists ? original.GetDedupedArtist() : original.Artist;
        var primaryArtist = options.DeriveAlbumArtist ? original.GetPrimaryArtist() : original.AlbumArtist;
        if (string.IsNullOrWhiteSpace(primaryArtist))
            primaryArtist = dedupedArtist;
        var sortForm = options.SetSortForm ? ToSortForm(primaryArtist) : original.AlbumArtistSort;

        var fixedTags = new TagInfo
        {
            Title = options.TrimWhitespace ? original.Title?.Trim() : original.Title,
            Artist = dedupedArtist,
            Album = options.TrimWhitespace ? original.Album?.Trim() : original.Album,
            AlbumArtist = primaryArtist,
            AlbumArtistSort = sortForm,
            Track = original.Track,
            TotalTracks = original.TotalTracks,
            Year = original.Year,
            Genre = options.FixGenre ? original.Genre?.Trim() : original.Genre,
            Composer = options.TrimWhitespace ? original.Composer?.Trim() : original.Composer,
            Comment = options.TrimWhitespace ? original.Comment?.Trim() : original.Comment,
        };

        WriteTags(filePath, fixedTags);
        return original;
    }

    public static void WriteTags(string filePath, TagInfo tags)
    {
        try
        {
            using var file = TagLib.File.Create(filePath);
            var tag = file.Tag;

            if (tags.Title != null) tag.Title = tags.Title;
            if (tags.Artist != null) tag.Performers = new[] { tags.Artist };
            if (tags.Album != null) tag.Album = tags.Album;
            if (tags.AlbumArtist != null) tag.AlbumArtists = new[] { tags.AlbumArtist };
            if (tags.AlbumArtistSort != null) tag.AlbumArtistsSort = new[] { tags.AlbumArtistSort };
            if (tags.Track.HasValue) tag.Track = (uint)tags.Track.Value;
            if (tags.TotalTracks.HasValue) tag.TrackCount = (uint)tags.TotalTracks.Value;
            if (tags.Year.HasValue) tag.Year = (uint)tags.Year.Value;
            if (tags.Genre != null) tag.Genres = new[] { tags.Genre };
            if (tags.Composer != null) tag.Composers = new[] { tags.Composer };
            if (tags.Comment != null) tag.Comment = tags.Comment;

            file.Save();
        }
        catch (Exception ex)
        {
            System.Diagnostics.Debug.WriteLine($"Failed to write tags to {filePath}: {ex.Message}");
            throw;
        }
    }

    private static string? ToSortForm(string? artist)
    {
        if (string.IsNullOrWhiteSpace(artist)) return null;
        var trimmed = artist.Trim();
        var articles = new[] { "The ", "A ", "An ", "Le ", "La ", "Les ", "El ", "Los " };
        foreach (var article in articles)
        {
            if (trimmed.StartsWith(article, StringComparison.OrdinalIgnoreCase))
            {
                var rest = trimmed.Substring(article.Length).Trim();
                if (!string.IsNullOrEmpty(rest))
                    return $"{rest}, {article.Trim()}";
            }
        }
        return trimmed;
    }
}
