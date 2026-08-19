using System.Windows;
using System.Windows.Input;

namespace ETuner;

public partial class MainWindow : Window
{
    public MainWindow()
    {
        InitializeComponent();
    }

    private void OnTitleBarDrag(object sender, MouseButtonEventArgs e)
    {
        DragMove();
    }

    private void OnThemeToggle(object sender, RoutedEventArgs e)
    {
        var vm = (ViewModels.MainViewModel)DataContext;
        vm.DarkThemeEnabled = !vm.DarkThemeEnabled;

        var appResources = System.Windows.Application.Current.Resources;
        var merged = appResources.MergedDictionaries;
        merged.Clear();

        if (vm.DarkThemeEnabled)
        {
            merged.Add(new ResourceDictionary { Source = new Uri("Themes/Dark.xaml", UriKind.Relative) });
        }
        else
        {
            merged.Add(new ResourceDictionary { Source = new Uri("Themes/Light.xaml", UriKind.Relative) });
        }
    }

    private void OnMinimize(object sender, RoutedEventArgs e)
    {
        WindowState = WindowState.Minimized;
    }

    private void OnClose(object sender, RoutedEventArgs e)
    {
        Close();
    }
}
