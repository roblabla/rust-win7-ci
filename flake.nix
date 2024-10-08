{
  description = "A very basic flake";

  inputs.flake-utils.url  = "github:numtide/flake-utils";
  inputs.poetry2nix.url = "github:nix-community/poetry2nix";
  inputs.poetry2nix.inputs.nixpkgs.follows = "nixpkgs";

  outputs = { self, nixpkgs, flake-utils, poetry2nix }:
    flake-utils.lib.eachDefaultSystem (system:
    let
      pkgs = import nixpkgs {
        inherit system;
        overlays = [ poetry2nix.overlays.default ];
      };
      pythonEnv = pkgs.poetry2nix.mkPoetryEnv {
        projectDir = ./.;
        overrides = pkgs.poetry2nix.overrides.withDefaults (self: super: {
          about-time = super.about-time.overridePythonAttrs (
            old: {
              nativeBuildInputs = old.nativeBuildInputs ++ [ self.setuptools ];
              postInstall = ''
                rm -f $out/LICENSE
              '';
            }
          );
          alive-progress = self.addBuildSystem "setuptools" super.alive-progress;
        });
      };
      # Similar to dockerTools.shadowSetup, but without needing to run in the
      # runAsRoot build step, which requires qemu and is fairly prone to
      # crashing.
      nonRootShadowSetup = { }: with pkgs; [
        (
        writeTextDir "etc/shadow" ''
          root:!x:::::::
        ''
        )
        (
        writeTextDir "etc/passwd" ''
          root:x:0:0::/root:${runtimeShell}
        ''
        )
        (
        writeTextDir "etc/group" ''
          root:x:0:
        ''
        )
        (
        writeTextDir "etc/gshadow" ''
          root:x::
        ''
        )
        (
        writeTextDir "etc/pam.d/other" ''
          account sufficient pam_unix.so
          auth sufficient pam_rootok.so
          password requisite pam_unix.so nullok yescrypt
          session required pam_unix.so
        ''
        )
        (writeTextDir "etc/login.defs" "")
      ];
    in
    {
      devShell = pkgs.mkShell {
        nativeBuildInputs = [
          # For clang-cl
          pkgs.llvmPackages_16.clang-unwrapped
          # For lld-link
          pkgs.llvmPackages_16.lld
          # For llvm-lib
          pkgs.llvmPackages_16.llvm

          pkgs.poetry

          pkgs.git

          pythonEnv

          pkgs.libiconv

          pkgs.openssh pkgs.sshpass pkgs.panamax
        ];
        shellHook = ''
            # Workaround a bug causing rust's patch_binaries_for_nix stuff to
            # break down.
            unset TMPDIR TEMPDIR TEMP TMP

            # Don't set the CI variable, as it makes a bunch of tests stricter
            # than we want them to be.
            unset CI

            # Set CC/CXX to the host cc/c++. By default on macos, it is set to
            # clang, which will be set to nix's clang-unwrapped. By setting it
            # to cc/c++, we avoid this issue.
            export CC=cc
            export CXX=c++
        '';
      };
      packages.dockerImage = pkgs.dockerTools.buildImageWithNixDb {
        name = "rustc-ci";
        tag = "v0"; # Overriden when we upload the docker anyway

        copyToRoot = pkgs.buildEnv {
          name = "rustc-ci-buildenv";
          paths = with pkgs; [
            # Package manager so we can install more tools if we need to in
            # the image.
            nix

            # Things the CI need for a successful build
            bash coreutils gnused gawk gnugrep findutils curl jq libarchive gnutar gzip

            # Rust minimal environment. We need the platform native compiler
            # and linker to work with proc macro and stuff.
            stdenv.cc stdenv.cc.bintools

            # For clang-cl
            llvmPackages_16.clang-unwrapped
            # For lld-link
            llvmPackages_16.lld
            # For llvm-lib
            llvmPackages_16.llvm

            pkgsCross.musl64.buildPackages.gcc-unwrapped

            poetry

            git

            pythonEnv

            libiconv

            openssh sshpass

            cacert
            (runCommand "ca-certificates" {} ''
              mkdir -p $out/etc/ssl/certs
              ln -s ${cacert}/etc/ssl/certs/ca-bundle.crt $out/etc/ssl/certs/ca-certificates.crt
            '')

            # Configuration files.
            (pkgs.writeTextDir "etc/gitconfig" ''
                [user]
                name = bot
                email = bot@example.com
            '')
            (pkgs.writeTextDir "etc/nix/nix.conf" "build-users-group = ")
            (pkgs.writeTextDir "etc/os-release" "ID=nixos")

            panamax
          ] ++ (nonRootShadowSetup {});
          pathsToLink = [
            "/bin"
            # cacert
            "/etc"
          ];
        };
        extraCommands = ''
          mkdir -p tmp
          mkdir -p usr
          ln -s /bin usr/bin
        '';
        config.Env = [
            "MUSL_LIBDIR=${pkgs.pkgsCross.musl64.buildPackages.gcc.libc}/lib"
            "NIX_PATH=nixpkgs=${nixpkgs}"
        ];
      };
    });
}
