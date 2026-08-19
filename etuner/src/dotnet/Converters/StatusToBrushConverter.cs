using System.Globalization;
using System.Windows.Data;
using System.Windows.Media;

namespace ETuner.Converters
{
    public class StatusToBrushConverter : IValueConverter
    {
        public object Convert(object value, Type targetType, object parameter, CultureInfo culture)
        {
            var status = value as string ?? "";
            return status switch
            {
"Pending" => new SolidColorBrush(System.Windows.Media.Color.FromRgb(0xB0, 0xB0, 0xB0)),
                "Renamed" => new SolidColorBrush(System.Windows.Media.Color.FromRgb(0x28, 0xA7, 0x45)),
                "Tagged" => new SolidColorBrush(System.Windows.Media.Color.FromRgb(0x00, 0x66, 0xCC)),
                "Error" => new SolidColorBrush(System.Windows.Media.Color.FromRgb(0xDC, 0x35, 0x45)),
                "Clean" => new SolidColorBrush(System.Windows.Media.Color.FromRgb(0x28, 0xA7, 0x45)),
                _ => new SolidColorBrush(System.Windows.Media.Color.FromRgb(0xB0, 0xB0, 0xB0))
            };
        }

        public Object ConvertBack(object value, Type targetType, object parameter, CultureInfo culture)
            => throw new NotImplementedException();
    }
}
