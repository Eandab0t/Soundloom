using ETuner.Models;
using TagLib;

namespace ETuner.Services;

public static class MetadataReader
{
    public static TagInfo? ReadTags(string filePath)
    {
        try
        {
            using var file = TagLib.File.Create(filePath);
            var tag = file.Tag;

            var info = new TagInfo
            {
                Title = tag.Title?.Trim(),
                Artist = tag.Performers?.FirstOrDefault() ?? tag.FirstPerformer ?? tag.FirstAlbumArtist,
                Album = tag.Album?.Trim(),
                AlbumArtist = tag.FirstAlbumArtist?.Trim(),
                AlbumArtistSort = tag.AlbumArtistsSort?.FirstOrDefault()?.Trim(),
                Track = tag.Track > 0 ? (int?)tag.Track : null,
                TotalTracks = tag.TrackCount > 0 ? (int?)tag.TrackCount : null,
                Year = tag.Year > 0 ? (int?)tag.Year : null,
                Genre = tag.FirstGenre?.Trim(),
                Composer = tag.FirstComposer?.Trim(),
                Comment = tag.Comment?.Trim(),
            };

            if (string.IsNullOrWhiteSpace(info.AlbumArtist) && !string.IsNullOrWhiteSpace(info.Artist))
            {
                info.AlbumArtist = info.GetPrimaryArtist();
            }

            return info;
        }
        catch (Exception ex)
        {
            System.Diagnostics.Debug.WriteLine($"Failed to read tags from {filePath}: {ex.Message}");
            return null;
        }
    }
}
