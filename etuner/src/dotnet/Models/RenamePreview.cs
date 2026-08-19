namespace ETuner.Models
{
    public class RenamePreview
    {
        public string OriginalName { get; set; } = "";
        public string DetectedCategory { get; set; } = "";
        public string Purpose { get; set; } = "";
        public string NewName { get; set; } = "";
        public string FullPath { get; set; } = "";
        public string NewFullPath { get; set; } = "";
        public bool IsSelected { get; set; } = true;
        public string Status { get; set; } = "Pending";
    }
}
