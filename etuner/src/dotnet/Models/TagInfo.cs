namespace ETuner.Models
{
    public class TagInfo
    {
        public string? Title { get; set; }
        public string? Artist { get; set; }
        public string? Album { get; set; }
        public string? AlbumArtist { get; set; }
        public string? AlbumArtistSort { get; set; }
        public int? Track { get; set; }
        public int? TotalTracks { get; set; }
        public int? Year { get; set; }
        public string? Genre { get; set; }
        public string? Composer { get; set; }
        public string? Comment { get; set; }

        public string? GetDedupedArtist()
        {
            if (string.IsNullOrWhiteSpace(Artist)) return null;
            var parts = Artist.Split(',').Select(p => p.Trim()).ToList();
            var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            var result = new List<string>();
            foreach (var p in parts)
            {
                if (!string.IsNullOrEmpty(p) && seen.Add(p.ToLowerInvariant()))
                    result.Add(p);
            }
            return result.Count > 0 ? string.Join(", ", result) : null;
        }

        public string? GetPrimaryArtist()
        {
            var deduped = GetDedupedArtist() ?? Artist;
            if (string.IsNullOrWhiteSpace(deduped)) return null;
            var parts = deduped.Split(',');
            return parts[0]?.Trim();
        }

        public bool HasDuplicateArtists()
        {
            if (string.IsNullOrWhiteSpace(Artist)) return false;
            var parts = Artist.Split(',').Select(p => p.Trim().ToLowerInvariant()).ToList();
            return parts.Count != parts.Distinct().Count();
        }

        public bool NeedsAlbumArtist()
        {
            return string.IsNullOrWhiteSpace(AlbumArtist) && !string.IsNullOrWhiteSpace(Artist);
        }

        public bool NeedsSortField()
        {
            return string.IsNullOrWhiteSpace(AlbumArtistSort) && !string.IsNullOrWhiteSpace(Artist);
        }
    }
}
