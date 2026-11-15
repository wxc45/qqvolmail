using System;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Reflection;
using System.Threading;
using System.Windows.Forms;

[assembly: AssemblyTitle("QQ 分卷邮递")]
[assembly: AssemblyDescription("QQ 分卷邮递单文件启动器")]
[assembly: AssemblyProduct("QQ 分卷邮递")]
[assembly: AssemblyCompany("QQ 分卷邮递")]
[assembly: AssemblyVersion("1.2.4.0")]
[assembly: AssemblyFileVersion("1.2.4.0")]

namespace QQVolMail
{
    internal static class Program
    {
        private const string AppVersion = "1.2.4";
        private const string ResourceName = "QQVolMail.App.zip";
        private const string AppFolderName = "QQ分卷邮递";
        private const string AppExeName = "QQ分卷邮递.exe";

        [STAThread]
        private static void Main()
        {
            Application.EnableVisualStyles();

            string home = Environment.GetEnvironmentVariable("QQVOLMAIL_HOME");
            if (string.IsNullOrEmpty(home))
            {
                home = Path.Combine(
                    Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location),
                    "QQVolMail_runtime");
            }

            string appRoot = Path.Combine(home, "app-" + AppVersion);
            string appExe = Path.Combine(appRoot, AppFolderName, AppExeName);
            string dataDir = Path.Combine(home, "data");
            string logPath = Path.Combine(home, "launcher.log");

            try
            {
                Directory.CreateDirectory(home);
                Directory.CreateDirectory(dataDir);

                if (!File.Exists(appExe))
                {
                    using (Mutex mutex = new Mutex(false, "QQVolMailLauncher_" + AppVersion))
                    {
                        if (!mutex.WaitOne(TimeSpan.FromMinutes(3)))
                        {
                            throw new TimeoutException("另一个启动器正在准备程序，请稍后重试。");
                        }
                        try
                        {
                            if (!File.Exists(appExe))
                            {
                                ExtractPayload(appRoot);
                            }
                        }
                        finally
                        {
                            mutex.ReleaseMutex();
                        }
                    }
                }

                if (!File.Exists(appExe))
                {
                    throw new FileNotFoundException("解压后未找到主程序。", appExe);
                }

                ProcessStartInfo startInfo = new ProcessStartInfo(appExe);
                startInfo.WorkingDirectory = Path.GetDirectoryName(appExe);
                startInfo.UseShellExecute = false;
                Environment.SetEnvironmentVariable(
                    "QQVOLMAIL_DATA_DIR",
                    dataDir,
                    EnvironmentVariableTarget.Process);
                Process.Start(startInfo);
            }
            catch (Exception ex)
            {
                WriteErrorLog(logPath, ex);
                MessageBox.Show(
                    "QQ 分卷邮递启动失败：\n" + ex.Message +
                    "\n\n日志文件：\n" + logPath +
                    "\n\n如果反复失败，请改用便携版压缩包。",
                    "QQ 分卷邮递",
                    MessageBoxButtons.OK,
                    MessageBoxIcon.Error);
            }
        }

        private static void ExtractPayload(string appRoot)
        {
            string tempRoot = appRoot + ".tmp";
            if (Directory.Exists(tempRoot))
            {
                Directory.Delete(tempRoot, true);
            }
            Directory.CreateDirectory(tempRoot);

            Assembly assembly = Assembly.GetExecutingAssembly();
            using (Stream stream = assembly.GetManifestResourceStream(ResourceName))
            {
                if (stream == null)
                {
                    throw new InvalidOperationException("启动器缺少内置程序资源。");
                }

                using (ZipArchive archive = new ZipArchive(stream, ZipArchiveMode.Read))
                {
                    string tempFull = Path.GetFullPath(tempRoot);
                    string tempPrefix = tempFull + Path.DirectorySeparatorChar;
                    foreach (ZipArchiveEntry entry in archive.Entries)
                    {
                        string destination = Path.GetFullPath(
                            Path.Combine(tempRoot, entry.FullName));
                        if (!destination.StartsWith(tempPrefix, StringComparison.OrdinalIgnoreCase))
                        {
                            throw new InvalidDataException("内置压缩包包含非法路径。");
                        }

                        if (entry.FullName.EndsWith("/") ||
                            entry.FullName.EndsWith("\\"))
                        {
                            Directory.CreateDirectory(destination);
                        }
                        else
                        {
                            string directory = Path.GetDirectoryName(destination);
                            if (!string.IsNullOrEmpty(directory))
                            {
                                Directory.CreateDirectory(directory);
                            }
                            entry.ExtractToFile(destination, true);
                        }
                    }
                }
            }

            if (Directory.Exists(appRoot))
            {
                Directory.Delete(appRoot, true);
            }
            Directory.Move(tempRoot, appRoot);
        }

        private static void WriteErrorLog(string logPath, Exception ex)
        {
            try
            {
                string directory = Path.GetDirectoryName(logPath);
                if (!string.IsNullOrEmpty(directory))
                {
                    Directory.CreateDirectory(directory);
                }
                File.AppendAllText(
                    logPath,
                    DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") +
                    " " + ex + Environment.NewLine);
            }
            catch
            {
            }
        }
    }
}

