using System;
using System.Collections.ObjectModel;
using System.ComponentModel;
using System.IO;
using System.Runtime.CompilerServices;
using System.Windows;
using System.Windows.Forms;
using System.Windows.Input;
using System.Threading.Tasks;
using System.Linq;
using System.Collections.Generic;
using ETuner.Models;
using ETuner.Services;

namespace ETuner.ViewModels
{
    public class MainViewModel : INotifyPropertyChanged
    {
        private readonly FileClassifier _classifier = new();
        private INamingStrategy _namingStrategy;
        private string _folderPath = "";
        private ObservableCollection<RenamePreview> _files = new();
        private ObservableCollection<INamingStrategy> _strategies = new();
        private bool _isScanning;
        private bool _isRenaming;
        private string _logOutput = "";
        private string _statusText = "Ready";

        private bool _tagCleanupEnabled = true;
        private bool _artistFixingEnabled = true;
        private bool _songSorterEnabled = true;

        private bool _dedupArtistsEnabled = true;
        private bool _deriveAlbumArtistEnabled = true;
        private bool _trimWhitespaceEnabled = true;
        private bool _fixGenreEnabled = true;
        private bool _fixTitleCaseEnabled = false;
        private bool _backupBeforeChanges = true;
        private bool _previewModeEnabled = true;
        private bool _recursiveScanEnabled = true;
        private bool _includeHiddenFiles = false;
        private bool _darkThemeEnabled = false;

        public string FolderPath
        {
            get => _folderPath;
            set { _folderPath = value; OnPropertyChanged(); }
        }

        public ObservableCollection<RenamePreview> Files
        {
            get => _files;
            set { _files = value; OnPropertyChanged(); }
        }

        public ObservableCollection<INamingStrategy> Strategies
        {
            get => _strategies;
            set { _strategies = value; OnPropertyChanged(); }
        }

        public INamingStrategy NamingStrategy
        {
            get => _namingStrategy;
            set { _namingStrategy = value; OnPropertyChanged(); OnPropertyChanged(nameof(NamingStrategy)); }
        }

        public bool IsScanning
        {
            get => _isScanning;
            set { _isScanning = value; OnPropertyChanged(); }
        }

        public bool IsRenaming
        {
            get => _isRenaming;
            set { _isRenaming = value; OnPropertyChanged(); }
        }

        public string LogOutput
        {
            get => _logOutput;
            set { _logOutput = value; OnPropertyChanged(); }
        }

        public string StatusText
        {
            get => _statusText;
            set { _statusText = value; OnPropertyChanged(); }
        }

        public bool TagCleanupEnabled
        {
            get => _tagCleanupEnabled;
            set { _tagCleanupEnabled = value; OnPropertyChanged(); }
        }

        public bool ArtistFixingEnabled
        {
            get => _artistFixingEnabled;
            set { _artistFixingEnabled = value; OnPropertyChanged(); }
        }

        public bool SongSorterEnabled
        {
            get => _songSorterEnabled;
            set { _songSorterEnabled = value; OnPropertyChanged(); }
        }

        public bool DedupArtistsEnabled
        {
            get => _dedupArtistsEnabled;
            set { _dedupArtistsEnabled = value; OnPropertyChanged(); }
        }

        public bool DeriveAlbumArtistEnabled
        {
            get => _deriveAlbumArtistEnabled;
            set { _deriveAlbumArtistEnabled = value; OnPropertyChanged(); }
        }

        public bool TrimWhitespaceEnabled
        {
            get => _trimWhitespaceEnabled;
            set { _trimWhitespaceEnabled = value; OnPropertyChanged(); }
        }

        public bool FixGenreEnabled
        {
            get => _fixGenreEnabled;
            set { _fixGenreEnabled = value; OnPropertyChanged(); }
        }

        public bool FixTitleCaseEnabled
        {
            get => _fixTitleCaseEnabled;
            set { _fixTitleCaseEnabled = value; OnPropertyChanged(); }
        }

        public bool BackupBeforeChanges
        {
            get => _backupBeforeChanges;
            set { _backupBeforeChanges = value; OnPropertyChanged(); }
        }

        public bool PreviewModeEnabled
        {
            get => _previewModeEnabled;
            set { _previewModeEnabled = value; OnPropertyChanged(); }
        }

        public bool RecursiveScanEnabled
        {
            get => _recursiveScanEnabled;
            set { _recursiveScanEnabled = value; OnPropertyChanged(); }
        }

        public bool IncludeHiddenFiles
        {
            get => _includeHiddenFiles;
            set { _includeHiddenFiles = value; OnPropertyChanged(); }
        }

        public bool DarkThemeEnabled
        {
            get => _darkThemeEnabled;
            set { _darkThemeEnabled = value; OnPropertyChanged(); }
        }

        public ICommand BrowseFolderCommand { get; }
        public ICommand RescanCommand { get; }
        public ICommand RenameAllCommand { get; }
        public ICommand PreviewCommand { get; }

        public MainViewModel()
        {
            _namingStrategy = NamingStrategyFactory.AllStrategies[0];
            Strategies = new ObservableCollection<INamingStrategy>(NamingStrategyFactory.AllStrategies);

            BrowseFolderCommand = new RelayCommand(_ => BrowseFolder());
            RescanCommand = new RelayCommand(_ => _ = Rescan(), _ => !IsScanning && !string.IsNullOrEmpty(FolderPath));
            RenameAllCommand = new RelayCommand(_ => _ = RenameAll(), _ => !IsRenaming && Files.Any(f => f.IsSelected));
            PreviewCommand = new RelayCommand(_ => UpdatePreviews(), _ => Files.Any());
        }

        private void Log(string message)
        {
            LogOutput += $"[{DateTime.Now:HH:mm:ss}] {message}\n";
        }

        private void BrowseFolder()
        {
            using var dlg = new FolderBrowserDialog
            {
                Description = "Select your music folder",
                SelectedPath = string.IsNullOrEmpty(FolderPath)
                    ? Environment.GetFolderPath(Environment.SpecialFolder.MyMusic)
                    : FolderPath,
                ShowNewFolderButton = true
            };

            if (dlg.ShowDialog() == DialogResult.OK)
            {
                FolderPath = dlg.SelectedPath;
                Log($"Selected folder: {FolderPath}");
            }
        }

        private async Task Rescan()
        {
            if (string.IsNullOrEmpty(FolderPath) || !Directory.Exists(FolderPath))
                return;

            IsScanning = true;
            StatusText = "Scanning...";
            Files.Clear();
            LogOutput = "";

            try
            {
                var classifications = await Task.Run(() =>
                    _classifier.ClassifyFolder(new DirectoryInfo(FolderPath), RecursiveScanEnabled, IncludeHiddenFiles));

                foreach (var c in classifications)
                {
                    var tags = c.Category == FileCategory.Audio
                        ? MetadataReader.ReadTags(c.FullPath)
                        : null;

                    var preview = new RenamePreview
                    {
                        FullPath = c.FullPath,
                        OriginalName = c.Name,
                        DetectedCategory = c.Category.ToString(),
                        Purpose = c.Purpose,
                        NewName = "",
                        IsSelected = true,
                        Status = "Pending"
                    };

                    Files.Add(preview);
                }

                UpdatePreviews();
                Log($"Scanned {Files.Count} files in {FolderPath}");
                StatusText = $"{Files.Count} files scanned";
            }
            catch (Exception ex)
            {
                Log($"Error: {ex.Message}");
                StatusText = "Scan failed";
            }
            finally
            {
                IsScanning = false;
            }
        }

        public void UpdatePreviews()
        {
            var index = 0;
            foreach (var f in Files)
            {
                var cat = Enum.TryParse<FileCategory>(f.DetectedCategory, out var c) ? c : FileCategory.Other;
                f.NewName = NamingStrategy.GenerateName(f, cat, index++);
                f.NewFullPath = Path.Combine(Path.GetDirectoryName(f.FullPath) ?? "", f.NewName);
            }
            CommandManager.InvalidateRequerySuggested();
        }

        private async Task RenameAll()
        {
            await Task.Run(() => {
                var selected = Files.Where(f => f.IsSelected).ToList();
                if (!selected.Any()) return;

                IsRenaming = true;
                StatusText = $"Processing {selected.Count} files...";

                var options = new TagFixOptions
                {
                    DedupArtists = ArtistFixingEnabled && DedupArtistsEnabled,
                    DeriveAlbumArtist = ArtistFixingEnabled && DeriveAlbumArtistEnabled,
                    SetSortForm = SongSorterEnabled,
                    TrimWhitespace = TagCleanupEnabled && TrimWhitespaceEnabled,
                    FixGenre = TagCleanupEnabled && FixGenreEnabled,
                    FixTitleCase = FixTitleCaseEnabled,
                };

                var backupFiles = new List<(string path, TagInfo original)>();
                foreach (var f in selected)
                {
                    if (f.DetectedCategory == "Audio")
                    {
                        var tags = MetadataReader.ReadTags(f.FullPath);
                        if (tags != null)
                            backupFiles.Add((f.FullPath, tags));
                    }
                }

                if (BackupBeforeChanges && backupFiles.Any())
                {
                    try
                    {
                        var backupPath = BackupService.CreateBackup(backupFiles);
                        Log($"Backup created: {backupPath}");
                    }
                    catch (Exception ex)
                    {
                        Log($"Backup failed: {ex.Message}");
                    }
                }

                var errors = 0;
                var successes = 0;
                var dryRun = PreviewModeEnabled;

                foreach (var f in selected)
                {
                    try
                    {
                        if (f.DetectedCategory == "Audio")
                        {
                            var tags = MetadataReader.ReadTags(f.FullPath);
                            if (tags != null)
                            {
                                if (!dryRun)
                                {
                                    MetadataWriter.ApplyFixes(f.FullPath, tags, options);
                                }
                                Log($"{(dryRun ? "Would fix" : "Fixed")} tags: {f.OriginalName}");
                            }
                        }

                        if (f.NewFullPath != f.FullPath && !string.IsNullOrEmpty(f.NewName))
                        {
                            if (dryRun)
                            {
                                f.Status = "Preview";
                                Log($"Would rename: {f.OriginalName} -> {f.NewName}");
                            }
                            else
                            {
                                var dest = f.NewFullPath;
                                var counter = 1;
                                while (File.Exists(dest))
                                {
                                    var dir = Path.GetDirectoryName(dest) ?? "";
                                    var nameNoExt = Path.GetFileNameWithoutExtension(f.NewName);
                                    var ext = Path.GetExtension(f.NewName);
                                    dest = Path.Combine(dir, $"{nameNoExt} ({counter++}){ext}");
                                }

                                File.Move(f.FullPath, dest);
                                f.Status = "Renamed";
                                Log($"Renamed: {f.OriginalName} -> {Path.GetFileName(dest)}");
                            }
                        }
                        else
                        {
                            f.Status = dryRun ? "Preview" : "Tagged";
                        }

                        successes++;
                    }
                    catch (Exception ex)
                    {
                        f.Status = "Error";
                        errors++;
                        Log($"Error processing {f.OriginalName}: {ex.Message}");
                    }
                }

                StatusText = dryRun
                    ? $"Preview: {successes} ok, {errors} errors"
                    : $"Done: {successes} ok, {errors} errors";
                Log($"{(dryRun ? "Preview" : "Rename")} complete: {successes} succeeded, {errors} failed");
                IsRenaming = false;
            });
        }

        public event PropertyChangedEventHandler? PropertyChanged;
        protected void OnPropertyChanged([CallerMemberName] string? name = null)
        {
            PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(name));
        }
    }
}
