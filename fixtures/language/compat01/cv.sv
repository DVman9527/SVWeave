module cv_top;
  wire y;
  assign y = x;       // use before declaration: tolerated by VCS, an error in strict slang
  wire x = 1'b1;
  string s;
  bit m;
  initial m = s.match("a.*"); // VCS string extension: slang rejects it even under --compat vcs
endmodule
