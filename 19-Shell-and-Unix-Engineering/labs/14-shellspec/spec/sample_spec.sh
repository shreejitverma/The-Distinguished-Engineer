Include subject.sh

Describe 'sample'
Describe 'greet'
It 'says hello'
When call greet world
The stdout should eq 'hello world'
End
End
It 'keeps a quote ('"'"') in the description'
When call bump
The variable COUNTER should eq 1
End
It 'reports an unhandled stdout as a warning'
When call greet unhandled
The status should be success
End
End
