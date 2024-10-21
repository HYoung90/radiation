[Setup]
AppName=Network Mirror Program
AppVersion=1.0
DefaultDirName={pf}\NetworkMirrorProgram
DefaultGroupName=Network Mirror Program
OutputDir=.\Output
OutputBaseFilename=Setup_NetworkMirrorProgram
Compression=lzma
SolidCompression=yes

[Files]
Source: "C:\Users\user\Desktop\Server\dist\mirror.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\Network Mirror Program"; Filename: "{app}\mirror.exe"

[Run]
Filename: "{app}\mirror.exe"; Description: "Launch Network Mirror Program"; Flags: nowait postinstall skipifsilent
