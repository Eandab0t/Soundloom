using System.ComponentModel;
using System.Runtime.CompilerServices;

namespace ETuner.Models
{
    public class FileClassification : INotifyPropertyChanged
    {
        private string _name = "";
        private string _extension = "";
        private string _fullPath = "";
        private long _size;
        private FileCategory _category;
        private string _purpose = "";
        private string _newName = "";
        private bool _isSelected;
        private string _status = "";

        public string Name
        {
            get => _name;
            set { _name = value; OnPropertyChanged(); }
        }

        public string Extension
        {
            get => _extension;
            set { _extension = value; OnPropertyChanged(); }
        }

        public string FullPath
        {
            get => _fullPath;
            set { _fullPath = value; OnPropertyChanged(); }
        }

        public string DirectoryName => System.IO.Path.GetDirectoryName(_fullPath) ?? "";

        public long Size
        {
            get => _size;
            set { _size = value; OnPropertyChanged(); }
        }

        public string SizeFormatted => $"{_size / 1024.0:F1} KB";

        public FileCategory Category
        {
            get => _category;
            set { _category = value; OnPropertyChanged(); }
        }

        public string CategoryDisplay => _category.ToString();

        public string Purpose
        {
            get => _purpose;
            set { _purpose = value; OnPropertyChanged(); }
        }

        public string NewName
        {
            get => _newName;
            set { _newName = value; OnPropertyChanged(); }
        }

        public bool IsSelected
        {
            get => _isSelected;
            set { _isSelected = value; OnPropertyChanged(); }
        }

        public string Status
        {
            get => _status;
            set { _status = value; OnPropertyChanged(); }
        }

        public event PropertyChangedEventHandler? PropertyChanged;
        protected void OnPropertyChanged([CallerMemberName] string? name = null)
        {
            PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(name));
        }
    }
}
